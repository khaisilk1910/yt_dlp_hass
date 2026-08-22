"""Signed HTTP endpoints for YouTube-DLP local files and remote stream relay."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Final

from aiohttp import ClientError, ClientResponse, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .playback import STREAM_URL_PREFIX, _mime_from_suffix

_UPSTREAM_RETRY_STATUSES: Final = frozenset({401, 403, 404, 410})
_RESPONSE_HEADERS: Final = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
)
_HOP_BY_HOP_HEADERS: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_STREAM_TIMEOUT = ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=60)


class YoutubeDlpMediaView(HomeAssistantView):
    """Serve library audio through Home Assistant signed media URLs."""

    url = "/api/yt_dlp/media/{location:.*}"
    name = "api:yt_dlp:media"
    requires_auth = True

    async def _path(self, hass: HomeAssistant, location: str) -> Path:
        from . import get_playback_manager

        manager = get_playback_manager(hass)
        path = await hass.async_add_executor_job(
            manager.resolve_library_file, location
        )
        if path is None:
            raise web.HTTPNotFound
        return path

    async def head(self, request: web.Request, location: str) -> web.Response:
        """Return media headers for renderers that probe with HEAD first."""
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, location)
        mime = mimetypes.guess_type(path.name)[0] or _mime_from_suffix(path.suffix)
        return web.Response(content_type=mime)

    async def get(self, request: web.Request, location: str) -> web.FileResponse:
        """Stream a local media file with aiohttp range support."""
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, location)
        return web.FileResponse(path)


class YoutubeDlpStreamView(HomeAssistantView):
    """Relay one yt-dlp upstream stream through Home Assistant.

    Cast and many other renderers cannot send yt-dlp's required request headers
    to a short-lived googlevideo URL. A signed HA URL keeps the renderer on the
    Home Assistant endpoint while HA performs the upstream request with the
    headers/cookies produced by yt-dlp.
    """

    url = f"{STREAM_URL_PREFIX}/{{token}}"
    name = "api:yt_dlp:stream"
    requires_auth = True

    async def head(self, request: web.Request, token: str) -> web.Response:
        """Probe the upstream with a one-byte GET and expose full media length."""
        response = await self._open_upstream(request, token, head_probe=True)
        try:
            headers = _copy_response_headers(response, head_probe=True)
            return web.Response(status=200, headers=headers)
        finally:
            response.release()

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        """Asynchronously relay a range-capable upstream response."""
        response = await self._open_upstream(request, token, head_probe=False)
        headers = _copy_response_headers(response, head_probe=False)
        outgoing = web.StreamResponse(status=response.status, headers=headers)
        prepared = False
        try:
            await outgoing.prepare(request)
            prepared = True
            async for chunk in response.content.iter_chunked(64 * 1024):
                await outgoing.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            # The renderer stopped/seeks and closed the current HTTP request.
            pass
        except asyncio.CancelledError:
            raise
        finally:
            response.release()
            if prepared:
                try:
                    await outgoing.write_eof()
                except (ConnectionResetError, RuntimeError):
                    pass
        return outgoing

    async def _open_upstream(
        self, request: web.Request, token: str, *, head_probe: bool
    ) -> ClientResponse:
        """Open upstream, refreshing the yt-dlp URL on common expiry/reject codes."""
        from . import get_playback_manager

        hass: HomeAssistant = request.app[KEY_HASS]
        manager = get_playback_manager(hass)
        client = async_get_clientsession(hass)

        # 1) current direct URL
        # 2) freshly resolve the same format route
        # 3) retry that route with android_vr excluded
        # 4) try another format with yt-dlp's normal client selection
        # 5) finally combine another format with the safe client selection
        #
        # The normal alternate route matters: a client-pinned extraction can have
        # fewer formats than yt-dlp's defaults and must not be allowed to mask a
        # usable stream with "Requested format is not available".
        attempts = (
            ("current", None, None),
            ("refresh", False, None),
            ("safe-client", False, True),
            ("alternate-route", True, False),
            ("safe-alternate-route", True, True),
        )
        last_error: Exception | None = None
        for label, advance_route, avoid_android_vr in attempts:
            if advance_route is None:
                stream_session = manager.get_stream_session(token)
            else:
                try:
                    stream_session = await manager.async_refresh_stream(
                        token,
                        advance_route=advance_route,
                        avoid_android_vr=avoid_android_vr,
                    )
                except Exception as err:  # noqa: BLE001 - continue with safe fallback
                    last_error = err
                    stream_session = None

            if stream_session is None:
                if label == "current":
                    raise web.HTTPNotFound(text="Playback stream has expired")
                continue

            headers = _upstream_headers(
                stream_session.info.http_headers,
                request,
                head_probe=head_probe,
            )
            try:
                response = await client.get(
                    stream_session.info.url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=_STREAM_TIMEOUT,
                    auto_decompress=False,
                )
            except (ClientError, TimeoutError, OSError) as err:
                last_error = err
                continue

            if response.status not in _UPSTREAM_RETRY_STATUSES:
                return response

            last_error = RuntimeError(
                f"upstream media rejected relay request with HTTP {response.status}"
            )
            response.release()

        if last_error is not None:
            raise web.HTTPBadGateway(
                text="Unable to open a playable upstream media stream"
            ) from last_error
        raise web.HTTPBadGateway(text="Upstream media stream was rejected")


def _upstream_headers(
    yt_headers: dict[str, str], request: web.Request, *, head_probe: bool
) -> dict[str, str]:
    """Build a scoped upstream request without forwarding HA client credentials."""
    headers = {
        key: value
        for key, value in yt_headers.items()
        if key.casefold() not in _HOP_BY_HOP_HEADERS
        and key.casefold() not in {"authorization", "proxy-authorization"}
    }

    if head_probe:
        headers["Range"] = "bytes=0-0"
    elif range_header := request.headers.get("Range"):
        headers["Range"] = range_header

    if if_range := request.headers.get("If-Range"):
        headers["If-Range"] = if_range
    return headers


def _copy_response_headers(
    response: ClientResponse, *, head_probe: bool
) -> dict[str, str]:
    """Copy only media response headers that are safe for the renderer."""
    headers = {
        name: response.headers[name]
        for name in _RESPONSE_HEADERS
        if name in response.headers
    }
    headers["Cache-Control"] = "no-store"

    if head_probe:
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                headers["Content-Length"] = total
        headers.pop("Content-Range", None)
        headers.setdefault("Accept-Ranges", "bytes")
    return headers
