"""Direct playback and local media library support for YouTube-DLP.

The download worker remains in manager.py. Remote playback is relayed through
Home Assistant so speakers do not need to fetch yt-dlp's short-lived upstream
URL directly. All yt-dlp extraction and filesystem scanning run in executors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path, PurePosixPath
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.importlib import async_import_module

from .favorites import FavoritesStore
from .helpers import (
    detect_javascript_runtime,
    normalize_download_directory,
    youtube_dl_class,
)

_LOGGER = logging.getLogger(__name__)
_JS_RUNTIME_UNSET = object()

AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".weba",
        ".webm",
    }
)
MAX_LIBRARY_ITEMS = 1000
LIBRARY_CACHE_SECONDS = 15.0
MEDIA_URL_PREFIX = "/api/yt_dlp/media"
STREAM_URL_PREFIX = "/api/yt_dlp/stream"
STREAM_MEDIA_SOURCE_PREFIX = "__stream__/"
STREAM_SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_STREAM_SESSIONS = 32

# Keep separate routes. A slash selector only falls back when a format is absent;
# it does not switch routes when a selected YouTube media URL later returns 403.
STREAM_FORMAT_SELECTORS = (
    "ba[ext=m4a]/ba[acodec^=mp4a]",
    "ba[ext=webm]/ba[acodec^=opus]",
    "b[ext=mp4]/b",
)


@dataclass(slots=True, frozen=True)
class StreamInfo:
    """Resolved stream metadata and the request data needed by HA's relay."""

    url: str
    title: str
    thumbnail: str | None
    artist: str | None
    duration: int | float | None
    mime_type: str
    webpage_url: str
    http_headers: dict[str, str]
    route_index: int
    avoid_android_vr: bool

    def as_dict(self) -> dict[str, Any]:
        """Return only user-facing, JSON-safe response data."""
        return {
            "url": self.webpage_url,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "artist": self.artist,
            "duration": self.duration,
            "mime_type": self.mime_type,
        }


@dataclass(slots=True)
class StreamSession:
    """Short-lived relay session for one source URL."""

    token: str
    source_url: str
    info: StreamInfo
    created_at: float
    last_access: float


class PlaybackManager:
    """Resolve remote audio, relay streams, and scan the local music library."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        library_path: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.library_path = normalize_download_directory(library_path)
        self._scan_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        self._library_cache: list[dict[str, Any]] | None = None
        self._library_cache_at = 0.0
        self._stream_sessions: dict[str, StreamSession] = {}
        self.favorites = FavoritesStore(hass)
        self._javascript_runtime: tuple[str, str] | None | object = _JS_RUNTIME_UNSET
        self._javascript_runtime_lock = threading.Lock()

    def _js_runtime_options(self) -> dict[str, dict[str, str]] | None:
        """Lazily detect a JS runtime inside an executor worker."""
        if self._javascript_runtime is _JS_RUNTIME_UNSET:
            with self._javascript_runtime_lock:
                if self._javascript_runtime is _JS_RUNTIME_UNSET:
                    self._javascript_runtime = detect_javascript_runtime()

        runtime = self._javascript_runtime
        if not isinstance(runtime, tuple):
            return None
        name, path = runtime
        return {name: {"path": path}}

    async def async_resolve_stream(
        self,
        url: str,
        route_indexes: tuple[int, ...] | None = None,
        *,
        avoid_android_vr: bool = False,
    ) -> StreamInfo:
        """Resolve one usable upstream route without downloading the media."""
        indexes = route_indexes or tuple(range(len(STREAM_FORMAT_SELECTORS)))
        async with self._stream_lock:
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            return await self.hass.async_add_executor_job(
                self._resolve_stream_sync,
                url,
                youtube_dl_cls,
                indexes,
                avoid_android_vr,
            )

    def _resolve_stream_sync(
        self,
        url: str,
        youtube_dl_cls: type[Any],
        route_indexes: tuple[int, ...],
        avoid_android_vr: bool,
    ) -> StreamInfo:
        """Blocking yt-dlp metadata extraction. Executor thread only."""
        from yt_dlp.utils import DownloadError

        last_error: Exception | None = None
        for route_index in route_indexes:
            if route_index < 0 or route_index >= len(STREAM_FORMAT_SELECTORS):
                continue

            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "format": STREAM_FORMAT_SELECTORS[route_index],
                "socket_timeout": 20,
                "retries": 3,
                "extractor_retries": 2,
            }
            if js_runtimes := self._js_runtime_options():
                opts["js_runtimes"] = js_runtimes
            if avoid_android_vr:
                # Safe playback retry only. Do not mix this with yt-dlp's
                # defaults: on 2026.08.x that can still select ANDROID_VR or
                # produce an empty requested-client set when no JS runtime is
                # installed. web_embedded is requested alone so the stream
                # handed to Home Assistant's relay is genuinely non-android_vr.
                opts["extractor_args"] = {
                    "youtube": {"player_client": ["web_embedded"]}
                }

            try:
                with youtube_dl_cls(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not isinstance(info, dict):
                        raise DownloadError("yt-dlp did not return media information")

                    stream_url = info.get("url")
                    if not isinstance(stream_url, str) or not stream_url.startswith(
                        ("http://", "https://")
                    ):
                        raise DownloadError(
                            "yt-dlp did not return a playable direct media URL"
                        )

                    headers: dict[str, str] = {}
                    raw_headers = info.get("http_headers")
                    if hasattr(raw_headers, "items"):
                        headers.update(
                            {
                                str(key): str(value)
                                for key, value in raw_headers.items()
                                if value is not None
                            }
                        )
                    # yt-dlp intentionally keeps cookies out of http_headers.
                    # Capture only the cookies scoped to this selected media URL.
                    try:
                        cookie_header = ydl.cookiejar.get_cookie_header(stream_url)
                    except (AttributeError, ValueError):
                        cookie_header = None
                    if cookie_header:
                        headers["Cookie"] = str(cookie_header)

                    ext = str(info.get("ext") or "").lower()
                    acodec = str(info.get("acodec") or "").lower()
                    mime_type = _stream_mime_type(ext, acodec)
                    thumbnail = _best_thumbnail(info)
                    title = str(
                        info.get("title") or info.get("fulltitle") or "YouTube audio"
                    )
                    artist_value = (
                        info.get("artist") or info.get("channel") or info.get("uploader")
                    )
                    artist = str(artist_value) if artist_value else None
                    webpage_url = str(
                        info.get("webpage_url") or info.get("original_url") or url
                    )

                    return StreamInfo(
                        url=stream_url,
                        title=title,
                        thumbnail=thumbnail,
                        artist=artist,
                        duration=info.get("duration"),
                        mime_type=mime_type,
                        webpage_url=webpage_url,
                        http_headers=headers,
                        route_index=route_index,
                        avoid_android_vr=avoid_android_vr,
                    )
            except DownloadError as err:
                last_error = err

        if last_error is not None:
            raise last_error
        raise DownloadError("No supported playback route is available")

    async def async_create_stream(self, url: str) -> tuple[StreamInfo, str]:
        """Create a Home Assistant Media Source ID for one remote stream."""
        info = await self.async_resolve_stream(url)

        # yt-dlp 2026 has seen intermittent android_vr GVS 403 regressions. The
        # normal/default extraction remains first, matching the known-good
        # integration. If the selected media URL itself identifies ANDROID_VR,
        # re-resolve once with that client excluded before handing the relay to a
        # speaker. This is network work only when the user presses Play.
        if _is_android_vr_stream_url(info.url):
            _LOGGER.warning(
                "YouTube selected an android_vr playback URL; resolving a "
                "non-android_vr route before sending media to the player"
            )
            safe_routes = (info.route_index,) + tuple(
                index
                for index in range(len(STREAM_FORMAT_SELECTORS))
                if index != info.route_index
            )
            info = await self.async_resolve_stream(
                url,
                safe_routes,
                avoid_android_vr=True,
            )

        now = time.monotonic()
        self._prune_stream_sessions(now)
        while True:
            token = secrets.token_urlsafe(24)
            if token not in self._stream_sessions:
                break
        self._stream_sessions[token] = StreamSession(
            token=token,
            source_url=url,
            info=info,
            created_at=now,
            last_access=now,
        )
        return info, f"media-source://yt_dlp/{STREAM_MEDIA_SOURCE_PREFIX}{token}"

    def get_stream_session(self, token: str) -> StreamSession | None:
        """Return a live relay session without doing network or filesystem I/O."""
        now = time.monotonic()
        self._prune_stream_sessions(now)
        session = self._stream_sessions.get(token)
        if session is not None:
            session.last_access = now
        return session

    async def async_refresh_stream(
        self,
        token: str,
        *,
        advance_route: bool,
        avoid_android_vr: bool | None = None,
    ) -> StreamSession | None:
        """Refresh an expired/rejected upstream URL, optionally changing route/client."""
        session = self.get_stream_session(token)
        if session is None:
            return None

        current = session.info.route_index
        if advance_route:
            indexes = tuple(range(current + 1, len(STREAM_FORMAT_SELECTORS)))
            if not indexes:
                indexes = tuple(range(len(STREAM_FORMAT_SELECTORS)))
        else:
            indexes = (current,)

        info = await self.async_resolve_stream(
            session.source_url,
            indexes,
            avoid_android_vr=(
                session.info.avoid_android_vr
                if avoid_android_vr is None
                else avoid_android_vr
            ),
        )
        session.info = info
        session.last_access = time.monotonic()
        return session

    def _prune_stream_sessions(self, now: float) -> None:
        """Bound relay state without creating any background cleanup task."""
        expired = [
            token
            for token, session in self._stream_sessions.items()
            if now - session.last_access > STREAM_SESSION_TTL_SECONDS
        ]
        for token in expired:
            self._stream_sessions.pop(token, None)

        if len(self._stream_sessions) <= MAX_STREAM_SESSIONS:
            return
        oldest = sorted(
            self._stream_sessions.values(), key=lambda session: session.last_access
        )
        for session in oldest[: len(self._stream_sessions) - MAX_STREAM_SESSIONS]:
            self._stream_sessions.pop(session.token, None)

    async def async_list_favorites(self) -> list[dict[str, Any]]:
        """Return persistent favorite YouTube metadata."""
        return await self.favorites.async_list()

    async def async_add_favorite(self, url: str) -> dict[str, Any]:
        """Resolve metadata for one YouTube URL and persist it as a favorite."""
        info = await self.async_resolve_stream(url)
        return await self.favorites.async_add(info.as_dict())

    async def async_remove_favorite(self, url: str) -> bool:
        """Remove one persistent favorite by its canonical webpage URL."""
        return await self.favorites.async_remove(url)

    async def async_scan_library(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Return cached local audio list, refreshing in an executor when needed."""
        now = time.monotonic()
        if (
            not force
            and self._library_cache is not None
            and now - self._library_cache_at < LIBRARY_CACHE_SECONDS
        ):
            return self._library_cache

        async with self._scan_lock:
            now = time.monotonic()
            if (
                not force
                and self._library_cache is not None
                and now - self._library_cache_at < LIBRARY_CACHE_SECONDS
            ):
                return self._library_cache

            items = await self.hass.async_add_executor_job(self._scan_library_sync)
            self._library_cache = items
            self._library_cache_at = time.monotonic()
            return items

    def _scan_library_sync(self) -> list[dict[str, Any]]:
        """Recursively scan supported music files. Executor thread only."""
        base = Path(self.library_path)
        if not base.is_dir():
            return []

        items: list[dict[str, Any]] = []
        try:
            candidates = base.rglob("*")
            for path in candidates:
                if len(items) >= MAX_LIBRARY_ITEMS:
                    break
                try:
                    relative = path.relative_to(base)
                except ValueError:
                    continue
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if path.is_symlink():
                    continue
                if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    continue

                relative_posix = relative.as_posix()
                mime_type = mimetypes.guess_type(path.name)[0] or _mime_from_suffix(
                    path.suffix
                )
                items.append(
                    {
                        "id": relative_posix,
                        "title": path.stem,
                        "filename": path.name,
                        "relative_path": relative_posix,
                        "media_content_id": (
                            "media-source://yt_dlp/"
                            f"{quote(relative_posix, safe='/')}"
                        ),
                        "mime_type": mime_type,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
        except OSError as err:
            _LOGGER.warning("Unable to scan YouTube-DLP media library %s: %s", base, err)
            return []

        # Downloads are usually what the user wants first; newest files therefore
        # appear at the top, with filename as a deterministic tie-breaker.
        items.sort(
            key=lambda item: (
                -float(item["modified"]),
                str(item["filename"]).casefold(),
            )
        )
        return items

    def resolve_library_file(self, relative_path: str) -> Path | None:
        """Safely map a relative path to a real file. Executor thread only."""
        try:
            pure = PurePosixPath(relative_path)
        except (TypeError, ValueError):
            return None
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            return None
        if Path(pure.name).suffix.lower() not in AUDIO_EXTENSIONS:
            return None

        try:
            base = Path(self.library_path).resolve(strict=True)
            candidate = base.joinpath(*pure.parts).resolve(strict=True)
            candidate.relative_to(base)
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not candidate.is_file() or candidate.is_symlink():
            return None
        return candidate


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    """Pick the best available thumbnail without exposing yt-dlp internals."""
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return item["url"]
    return None


def _is_android_vr_stream_url(url: str) -> bool:
    """Return whether a selected GoogleVideo URL identifies the android_vr client."""
    try:
        client = parse_qs(urlsplit(url).query).get("c", [])
    except ValueError:
        return False
    return any(str(value).casefold() == "android_vr" for value in client)


def _stream_mime_type(ext: str, acodec: str) -> str:
    """Return a speaker-friendly MIME type for a selected yt-dlp format."""
    if ext in ("m4a", "mp4") or acodec.startswith("mp4a"):
        return "audio/mp4"
    if ext in ("webm", "weba"):
        return "audio/webm"
    if ext in ("ogg", "oga", "opus") or "opus" in acodec:
        return "audio/ogg"
    if ext == "mp3":
        return "audio/mpeg"
    if ext == "flac":
        return "audio/flac"
    if ext == "wav":
        return "audio/wav"
    return "music"


def _mime_from_suffix(suffix: str) -> str:
    """Return common MIME types missing on some minimal Python images."""
    return {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".opus": "audio/ogg",
        ".oga": "audio/ogg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".aac": "audio/aac",
        ".weba": "audio/webm",
        ".webm": "audio/webm",
    }.get(suffix.lower(), "music")
