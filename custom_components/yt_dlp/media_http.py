"""HTTP endpoints for YouTube-DLP local files and remote stream relay."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Final

from aiohttp import ClientError, ClientResponse, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .playback import STREAM_URL_PREFIX, _mime_from_suffix

_LOGGER = logging.getLogger(__name__)
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
_MAX_RELAY_CANDIDATES_PER_CLIENT: Final = 12


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
    to a short-lived googlevideo URL. A short-lived capability URL keeps the
    renderer on the Home Assistant endpoint while HA performs the upstream request with the
    headers/cookies produced by yt-dlp.
    """

    # The stream token is a high-entropy, short-lived capability. Keeping this
    # endpoint free of Home Assistant's long authSig query is intentional: a
    # number of Cast receivers are unreliable with long signed media URLs.
    # The token remains unguessable and expires with the in-memory session.
    url = f"{STREAM_URL_PREFIX}/{{token_and_ext}}"
    name = "api:yt_dlp:stream"
    requires_auth = False

    async def head(
        self, request: web.Request, token_and_ext: str
    ) -> web.Response:
        """Probe the upstream with a one-byte GET and expose full media length."""
        token = _stream_token(token_and_ext)
        response = await self._open_upstream(request, token, head_probe=True)
        try:
            headers = _copy_response_headers(response, head_probe=True)
            return web.Response(status=200, headers=headers)
        finally:
            response.release()

    async def get(
        self, request: web.Request, token_and_ext: str
    ) -> web.StreamResponse:
        """Asynchronously relay a range-capable upstream response."""
        token = _stream_token(token_and_ext)
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
        """Open upstream and walk real lower-quality formats when needed."""
        from . import get_playback_manager

        hass: HomeAssistant = request.app[KEY_HASS]
        manager = get_playback_manager(hass)
        client = async_get_clientsession(hass)
        stream_session = manager.get_stream_session(token)
        if stream_session is None:
            raise web.HTTPNotFound(text="Playback stream has expired")

        last_error: Exception | None = None

        async def _open_current() -> ClientResponse | None:
            nonlocal last_error
            current_session = manager.get_stream_session(token)
            if current_session is None:
                return None
            headers = _upstream_headers(
                current_session.info.http_headers,
                request,
                head_probe=head_probe,
            )
            try:
                response = await client.get(
                    current_session.info.url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=_STREAM_TIMEOUT,
                    auto_decompress=False,
                )
            except (ClientError, TimeoutError, OSError) as err:
                last_error = err
                return None

            # Redirects are already followed. Any remaining 4xx/5xx response is
            # unusable for Cast, regardless of whether YouTube used one of the
            # small set of status codes seen in older releases.
            if response.status < 400:
                return response

            last_error = RuntimeError(
                f"upstream media rejected relay request with HTTP {response.status}"
            )
            response.release()
            return None

        # First use the URL that was already probed before play_media. This avoids
        # an unnecessary YouTube extraction for the common path.
        response = await _open_current()
        if response is not None:
            return response

        initial_clients = stream_session.info.player_clients
        profiles: list[tuple[str, ...] | None] = [initial_clients]
        for profile in (
            ("default", "web_embedded"),
            ("android_vr",),
            ("web_embedded",),
            ("web_safari",),
        ):
            if profile not in profiles:
                profiles.append(profile)

        # For each client, refresh the best candidate then keep moving down the
        # actual extracted list. This is deliberately different from the old
        # fixed ba/ba.2/b ladder: if YouTube changes the list, the relay still
        # walks whatever direct formats really exist at that moment.
        for profile in profiles:
            for candidate_index in range(_MAX_RELAY_CANDIDATES_PER_CLIENT):
                try:
                    refreshed = await manager.async_refresh_stream(
                        token,
                        advance_route=candidate_index > 0,
                        player_clients=profile,
                    )
                except Exception as err:  # noqa: BLE001 - yt-dlp public errors vary
                    last_error = err
                    break
                if refreshed is None:
                    break

                response = await _open_current()
                if response is not None:
                    return response

        if last_error is not None:
            _LOGGER.warning(
                "YouTube-DLP stream relay failed after real-format fallbacks: %s",
                last_error,
            )
            raise web.HTTPBadGateway(
                text="Unable to open a playable upstream media stream"
            ) from last_error
        raise web.HTTPBadGateway(text="Upstream media stream was rejected")


def _stream_token(token_and_ext: str) -> str:
    """Strip the cosmetic media suffix from a capability token."""
    token = token_and_ext.split(".", 1)[0]
    if not token or "/" in token:
        raise web.HTTPNotFound(text="Playback stream has expired")
    return token


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
    headers["Access-Control-Allow-Origin"] = "*"
    headers["Cross-Origin-Resource-Policy"] = "cross-origin"

    if head_probe:
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                headers["Content-Length"] = total
        headers.pop("Content-Range", None)
        headers.setdefault("Accept-Ranges", "bytes")
    return headers
