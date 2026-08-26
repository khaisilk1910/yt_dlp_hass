"""Isolated TV video resolver and HTTP relay.

This module is optional by design. It is never imported by the protected
v0.5.16 download/play service modules, so a TV-specific regression cannot stop
core service registration or Home Assistant startup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any, Final

from aiohttp import ClientError, ClientResponse, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.importlib import async_import_module

from .helpers import detect_javascript_runtime, youtube_dl_class
from .js_runtime import async_ensure_javascript_runtime
from .playback import YOUTUBE_CLIENT_FALLBACKS

_LOGGER = logging.getLogger(__name__)

TV_STREAM_URL_PREFIX = "/api/yt_dlp/tv"
_TV_SESSION_TTL = 6 * 60 * 60
_MAX_TV_SESSIONS = 16
_TV_MAX_ROUTES = 24
_TV_PROBE_TIMEOUT = ClientTimeout(total=20, connect=10, sock_connect=10, sock_read=10)
_TV_STREAM_TIMEOUT = ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=60)
_RETRY_STATUSES: Final = frozenset({401, 403, 404, 410, 416, 429})
_HOP_HEADERS: Final = frozenset(
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


@dataclass(slots=True, frozen=True)
class TvStreamInfo:
    """Resolved progressive/muxed video stream."""

    url: str
    title: str
    thumbnail: str | None
    duration: int | float | None
    mime_type: str
    http_headers: dict[str, str]
    route_index: int
    player_clients: tuple[str, ...] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "mime_type": self.mime_type,
        }


@dataclass(slots=True)
class TvStreamSession:
    token: str
    source_url: str
    info: TvStreamInfo
    created_at: float
    last_access: float


class TvPlaybackManager:
    """Resolve TV video only when the user explicitly asks to play on a TV."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._lock = asyncio.Lock()
        self._sessions: dict[str, TvStreamSession] = {}
        self._js_runtime: tuple[str, str] | None | object = _UNSET

    async def async_create_stream(self, source_url: str) -> tuple[TvStreamInfo, str]:
        """Resolve/probe a TV-safe muxed stream and create a capability URL."""
        info = await self._async_resolve_playable(source_url)
        now = time.monotonic()
        self._prune(now)
        while True:
            token = secrets.token_urlsafe(24)
            if token not in self._sessions:
                break
        self._sessions[token] = TvStreamSession(token, source_url, info, now, now)
        suffix = ".webm" if info.mime_type == "video/webm" else ".mp4"
        return info, f"{TV_STREAM_URL_PREFIX}/{token}{suffix}?source=yt_dlp"

    def get_session(self, token: str) -> TvStreamSession | None:
        now = time.monotonic()
        self._prune(now)
        session = self._sessions.get(token)
        if session is not None:
            session.last_access = now
        return session

    async def async_refresh(self, token: str) -> TvStreamSession | None:
        session = self.get_session(token)
        if session is None:
            return None
        start = min(session.info.route_index + 1, _TV_MAX_ROUTES - 1)
        info = await self._async_resolve_playable(
            session.source_url,
            route_order=tuple(range(start, _TV_MAX_ROUTES)) + tuple(range(0, start)),
        )
        session.info = info
        session.last_access = time.monotonic()
        return session

    async def _async_resolve_playable(
        self, source_url: str, *, route_order: tuple[int, ...] | None = None
    ) -> TvStreamInfo:
        last_error: Exception | None = None
        indexes = route_order or tuple(range(_TV_MAX_ROUTES))
        async with self._lock:
            await async_ensure_javascript_runtime(self.hass)
            self._javascript_runtime = _JS_RUNTIME_UNSET
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            for clients in YOUTUBE_CLIENT_FALLBACKS:
                try:
                    candidates = await self.hass.async_add_executor_job(
                        self._resolve_sync, source_url, youtube_dl_cls, clients
                    )
                except Exception as err:  # noqa: BLE001
                    last_error = err
                    continue
                for index in indexes:
                    if index >= len(candidates):
                        continue
                    info = candidates[index]
                    try:
                        status = await self._probe(info)
                    except (ClientError, TimeoutError, OSError) as err:
                        last_error = err
                        continue
                    if status in {200, 206}:
                        return info
                    last_error = RuntimeError(f"TV stream probe returned HTTP {status}")
                    if status in {401, 429}:
                        break
        if last_error is not None:
            raise last_error
        raise RuntimeError("No playable muxed YouTube video stream is available")

    def _resolve_sync(
        self,
        source_url: str,
        youtube_dl_cls: type[Any],
        player_clients: tuple[str, ...],
    ) -> list[TvStreamInfo]:
        from yt_dlp.utils import DownloadError

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignore_no_formats_error": True,
            "socket_timeout": 20,
            "retries": 3,
            "extractor_retries": 3,
            "extractor_args": {"youtube": {"player_client": list(player_clients)}},
        }
        runtime = self._detect_js_runtime_sync()
        if runtime:
            name, path = runtime
            opts["js_runtimes"] = {name: {"path": path}}

        def number(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        def rank(item: dict[str, Any]) -> tuple[float, ...]:
            ext = str(item.get("ext") or "").lower()
            acodec = str(item.get("acodec") or "").lower()
            vcodec = str(item.get("vcodec") or "").lower()
            h264 = vcodec.startswith(("avc1", "h264"))
            aac = acodec.startswith("mp4a") or "aac" in acodec
            compatibility = 5.0 if ext == "mp4" and h264 and aac else 4.0 if ext == "mp4" and h264 else 3.0 if ext == "mp4" else 2.0 if ext == "webm" else 1.0
            height = number(item.get("height"))
            # 1080p and below is the broadest direct-play target. Higher streams
            # are retained but rank below compatible 1080p to avoid TV decoder issues.
            height_score = height if height <= 1080 else 1080 - (height - 1080) / 10
            return (compatibility, height_score, number(item.get("fps")), number(item.get("tbr")))

        with youtube_dl_cls(opts) as ydl:
            info = ydl.extract_info(source_url, download=False, process=False)
            if not isinstance(info, dict):
                raise DownloadError("yt-dlp did not return video information")
            formats = info.get("formats")
            raw = list(formats) if isinstance(formats, list) else []
            if isinstance(info.get("url"), str):
                raw.append(info)

            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
                    continue
                if bool(item.get("has_drm")):
                    continue
                protocol = str(item.get("protocol") or "").lower()
                if protocol and protocol not in {"http", "https"}:
                    continue
                if str(item.get("vcodec") or "none").lower() in {"", "none"}:
                    continue
                if str(item.get("acodec") or "none").lower() in {"", "none"}:
                    continue
                seen.add(url)
                candidates.append(item)
            candidates.sort(key=rank, reverse=True)
            if not candidates:
                raise DownloadError("No progressive/muxed video format is available")

            title = str(info.get("title") or info.get("fulltitle") or "YouTube video")
            thumbnail = _best_thumbnail(info)
            result: list[TvStreamInfo] = []
            for route_index, item in enumerate(candidates[:_TV_MAX_ROUTES]):
                stream_url = str(item["url"])
                headers: dict[str, str] = {}
                for raw_headers in (info.get("http_headers"), item.get("http_headers")):
                    if hasattr(raw_headers, "items"):
                        headers.update({str(k): str(v) for k, v in raw_headers.items() if v is not None})
                try:
                    cookie = ydl.cookiejar.get_cookie_header(stream_url)
                except (AttributeError, ValueError):
                    cookie = None
                if cookie:
                    headers["Cookie"] = str(cookie)
                ext = str(item.get("ext") or "").lower()
                result.append(
                    TvStreamInfo(
                        url=stream_url,
                        title=title,
                        thumbnail=thumbnail,
                        duration=info.get("duration"),
                        mime_type="video/mp4" if ext == "mp4" else "video/webm" if ext == "webm" else "video/mp4",
                        http_headers=headers,
                        route_index=route_index,
                        player_clients=player_clients,
                    )
                )
            return result

    def _detect_js_runtime_sync(self) -> tuple[str, str] | None:
        if self._js_runtime is _UNSET:
            self._js_runtime = detect_javascript_runtime()
        return self._js_runtime if isinstance(self._js_runtime, tuple) else None

    async def _probe(self, info: TvStreamInfo) -> int:
        headers = _clean_headers(info.http_headers)
        headers["Range"] = "bytes=0-0"
        response = await async_get_clientsession(self.hass).get(
            info.url,
            headers=headers,
            allow_redirects=True,
            timeout=_TV_PROBE_TIMEOUT,
            auto_decompress=False,
        )
        try:
            return response.status
        finally:
            response.release()

    def _prune(self, now: float) -> None:
        expired = [token for token, session in self._sessions.items() if now - session.last_access > _TV_SESSION_TTL]
        for token in expired:
            self._sessions.pop(token, None)
        if len(self._sessions) > _MAX_TV_SESSIONS:
            oldest = sorted(self._sessions.values(), key=lambda item: item.last_access)
            for session in oldest[: len(self._sessions) - _MAX_TV_SESSIONS]:
                self._sessions.pop(session.token, None)


_UNSET = object()
_TV_DATA_KEY = "yt_dlp_tv_playback"


def get_tv_manager(hass: HomeAssistant) -> TvPlaybackManager:
    manager = hass.data.get(_TV_DATA_KEY)
    if not isinstance(manager, TvPlaybackManager):
        manager = TvPlaybackManager(hass)
        hass.data[_TV_DATA_KEY] = manager
    return manager


class YoutubeDlpTvStreamView(HomeAssistantView):
    """Relay TV video through HA with Range support and no auth cookie dependency."""

    url = f"{TV_STREAM_URL_PREFIX}/{{token_and_ext}}"
    name = "api:yt_dlp:tv_stream"
    requires_auth = False

    async def head(self, request: web.Request, token_and_ext: str) -> web.Response:
        response = await self._open(request, _token(token_and_ext), head_probe=True)
        try:
            headers = _copy_headers(response, head_probe=True)
            return web.Response(status=200, headers=headers)
        finally:
            response.release()

    async def get(self, request: web.Request, token_and_ext: str) -> web.StreamResponse:
        response = await self._open(request, _token(token_and_ext), head_probe=False)
        headers = _copy_headers(response, head_probe=False)
        outgoing = web.StreamResponse(status=response.status, headers=headers)
        prepared = False
        try:
            await outgoing.prepare(request)
            prepared = True
            async for chunk in response.content.iter_chunked(128 * 1024):
                await outgoing.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            response.release()
            if prepared:
                try:
                    await outgoing.write_eof()
                except (ConnectionResetError, RuntimeError):
                    pass
        return outgoing

    async def _open(self, request: web.Request, token: str, *, head_probe: bool) -> ClientResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        manager = get_tv_manager(hass)
        client = async_get_clientsession(hass)
        last_error: Exception | None = None
        for attempt in range(2):
            session = manager.get_session(token) if attempt == 0 else await manager.async_refresh(token)
            if session is None:
                raise web.HTTPNotFound(text="TV stream has expired")
            headers = _clean_headers(session.info.http_headers)
            if head_probe:
                headers["Range"] = "bytes=0-0"
            elif range_header := request.headers.get("Range"):
                headers["Range"] = range_header
            try:
                response = await client.get(
                    session.info.url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=_TV_STREAM_TIMEOUT,
                    auto_decompress=False,
                )
            except (ClientError, TimeoutError, OSError) as err:
                last_error = err
                continue
            if response.status not in _RETRY_STATUSES:
                return response
            last_error = RuntimeError(f"upstream TV stream returned HTTP {response.status}")
            response.release()
        raise web.HTTPBadGateway(text=f"Unable to open TV stream: {last_error}")


def _token(value: str) -> str:
    token = value.split(".", 1)[0]
    if not token or "/" in token:
        raise web.HTTPNotFound
    return token


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.casefold() not in _HOP_HEADERS
        and key.casefold() not in {"authorization", "proxy-authorization"}
    }


def _copy_headers(response: ClientResponse, *, head_probe: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if name in response.headers:
            result[name] = response.headers[name]
    result.setdefault("Content-Type", "video/mp4")
    result.setdefault("Accept-Ranges", "bytes")
    result["Cache-Control"] = "no-store"
    result["Access-Control-Allow-Origin"] = "*"
    result["transferMode.dlna.org"] = "Streaming"
    result["contentFeatures.dlna.org"] = "DLNA.ORG_OP=01;DLNA.ORG_CI=0"
    if head_probe:
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                result["Content-Length"] = total
        result.pop("Content-Range", None)
    return result


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return str(item["url"])
    value = info.get("thumbnail")
    return str(value) if isinstance(value, str) else None
