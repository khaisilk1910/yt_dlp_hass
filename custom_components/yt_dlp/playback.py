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
import re
from pathlib import Path, PurePosixPath
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.importlib import async_import_module

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
_STREAM_PROBE_TIMEOUT = ClientTimeout(
    total=20, connect=10, sock_connect=10, sock_read=10
)
_STREAM_PROBE_OK = frozenset({200, 206})
_STREAM_PROBE_SWITCH_CLIENT = frozenset({401, 429})
_MAX_AUDIO_QUALITY_ROUTES = 32
_MAX_MUXED_QUALITY_ROUTES = 16
_MAX_PLAYER_RELOAD_RETRIES = 3
_PLAYER_RELOAD_RETRY_DELAY_SECONDS = 0.6

# YouTube currently has an intermittent player-response failure that surfaces as
# "The page needs to be reloaded" before format selection even starts.  Start
# with yt-dlp's documented default + web_embedded combination, then retry via
# independent no/low-PO-token client paths.  Do not use the logged-out tv client
# as a fallback: current yt-dlp guidance notes that its formats may be DRM-only.
YOUTUBE_CLIENT_FALLBACKS: tuple[tuple[str, ...], ...] = (
    ("default", "web_embedded"),
    ("android_vr",),
    ("web_embedded",),
    ("web_safari",),
)

# Keep every quality step as a separate route. A slash-separated yt-dlp format
# expression only falls back when a format is absent; it does not move to the
# next-lower format when the selected GoogleVideo URL itself later returns 403.
# Separate ``ba.N`` routes let both the initial probe and the relay refresh walk
# from the best audio stream down through lower qualities. Progressive/muxed
# ``b.N`` routes are the final fallback for sparse client profiles (for example
# profiles that expose only format 18).
STREAM_FORMAT_SELECTORS = tuple(
    ["ba"]
    + [f"ba.{index}" for index in range(2, _MAX_AUDIO_QUALITY_ROUTES + 1)]
    + ["b"]
    + [f"b.{index}" for index in range(2, _MAX_MUXED_QUALITY_ROUTES + 1)]
)
_FIRST_MUXED_ROUTE_INDEX = _MAX_AUDIO_QUALITY_ROUTES


@dataclass(slots=True, frozen=True)
class StreamInfo:
    """Resolved stream metadata and the request data needed by HA's relay."""

    url: str
    title: str
    thumbnail: str | None
    artist: str | None
    duration: int | float | None
    mime_type: str
    file_format: str
    webpage_url: str
    http_headers: dict[str, str]
    route_index: int
    player_clients: tuple[str, ...] | None

    def as_dict(self) -> dict[str, Any]:
        """Return only user-facing, JSON-safe response data."""
        return {
            "url": self.webpage_url,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "artist": self.artist,
            "duration": self.duration,
            "mime_type": self.mime_type,
            "file_format": self.file_format,
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
        self._library_metadata_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._stream_sessions: dict[str, StreamSession] = {}
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
        player_clients: tuple[str, ...] | None = None,
    ) -> StreamInfo:
        """Resolve one usable upstream route without downloading the media."""
        indexes = route_indexes or tuple(range(len(STREAM_FORMAT_SELECTORS)))
        async with self._stream_lock:
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            reload_attempt = 0
            while True:
                try:
                    return await self.hass.async_add_executor_job(
                        self._resolve_stream_sync,
                        url,
                        youtube_dl_cls,
                        indexes,
                        player_clients,
                    )
                except Exception as err:  # noqa: BLE001 - yt-dlp public errors vary
                    if (
                        _page_reload_error(err)
                        and reload_attempt < _MAX_PLAYER_RELOAD_RETRIES - 1
                    ):
                        reload_attempt += 1
                        _LOGGER.warning(
                            "YouTube player requested a page reload during stream "
                            "extraction (client=%s, retry %s/%s); retrying with a "
                            "fresh yt-dlp session",
                            ",".join(player_clients or ("default",)),
                            reload_attempt + 1,
                            _MAX_PLAYER_RELOAD_RETRIES,
                        )
                        await asyncio.sleep(
                            _PLAYER_RELOAD_RETRY_DELAY_SECONDS * reload_attempt
                        )
                        continue
                    raise

    def _resolve_stream_sync(
        self,
        url: str,
        youtube_dl_cls: type[Any],
        route_indexes: tuple[int, ...],
        player_clients: tuple[str, ...] | None,
    ) -> StreamInfo:
        """Blocking yt-dlp metadata extraction. Executor thread only."""
        from yt_dlp.utils import DownloadError

        last_error: Exception | None = None
        indexes = tuple(
            index
            for index in route_indexes
            if 0 <= index < len(STREAM_FORMAT_SELECTORS)
        )
        position = 0
        while position < len(indexes):
            route_index = indexes[position]

            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "format": STREAM_FORMAT_SELECTORS[route_index],
                # Ask yt-dlp to reject selected URLs that cannot actually be
                # opened. This makes `ba`/`b` automatically fall through to a
                # lower downloadable format before the URL is handed to Cast.
                "check_formats": "selected",
                "socket_timeout": 20,
                "retries": 3,
                "extractor_retries": 2,
            }
            if js_runtimes := self._js_runtime_options():
                opts["js_runtimes"] = js_runtimes
            if player_clients:
                # Use only positive client names. A negative/default expression
                # can become an empty set when yt-dlp changes its default client
                # preset, which produces "No player clients have been requested".
                opts["extractor_args"] = {
                    "youtube": {"player_client": list(player_clients)}
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
                    vcodec = str(info.get("vcodec") or "").lower()
                    mime_type = _stream_mime_type(ext, acodec, vcodec)
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
                        file_format=ext or _format_from_mime(mime_type),
                        webpage_url=webpage_url,
                        http_headers=headers,
                        route_index=route_index,
                        player_clients=player_clients,
                    )
            except DownloadError as err:
                last_error = err
                if _requested_format_unavailable(err):
                    # If one ba.N tier is absent, every later ba tier is absent
                    # too. Relay refreshes may pass a long route list, so jump
                    # straight to the first muxed/progressive route instead of
                    # performing dozens of doomed YouTube extractions.
                    if route_index < _FIRST_MUXED_ROUTE_INDEX:
                        muxed_position = next(
                            (
                                candidate_position
                                for candidate_position in range(position + 1, len(indexes))
                                if indexes[candidate_position] >= _FIRST_MUXED_ROUTE_INDEX
                            ),
                            None,
                        )
                        if muxed_position is not None:
                            position = muxed_position
                            continue
                    else:
                        break
                position += 1

        if last_error is not None:
            raise last_error
        raise DownloadError("No supported playback route is available")

    async def async_create_stream(self, url: str) -> tuple[StreamInfo, str]:
        """Create a Home Assistant Media Source ID for one verified remote stream."""
        # Verify the selected GoogleVideo URL from Home Assistant before handing
        # it to Cast. YouTube increasingly rejects some client/format URLs with
        # HTTP 403. A one-byte range probe lets us switch to a positive, known
        # fallback client before the receiver sees a broken media URL.
        info = await self._async_resolve_playable_stream(url)

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

    async def _async_resolve_playable_stream(self, url: str) -> StreamInfo:
        """Resolve and probe a stream using bounded client/format fallbacks."""
        last_error: Exception | None = None

        for player_clients in YOUTUBE_CLIENT_FALLBACKS:
            switch_client = False
            route_index = 0
            while route_index < len(STREAM_FORMAT_SELECTORS):
                try:
                    info = await self.async_resolve_stream(
                        url,
                        (route_index,),
                        player_clients=player_clients,
                    )
                except Exception as err:  # noqa: BLE001 - yt-dlp raises several public errors
                    last_error = err
                    if _no_player_clients_error(err):
                        switch_client = True
                        break
                    if _requested_format_unavailable(err):
                        # If the n'th audio-only format does not exist, no later
                        # ba.N route can exist either. Jump straight to the best
                        # progressive/muxed fallback. Likewise, once b.N is
                        # absent there is nothing lower left on this client.
                        if route_index < _FIRST_MUXED_ROUTE_INDEX:
                            route_index = _FIRST_MUXED_ROUTE_INDEX
                            continue
                        break
                    if _retry_lower_stream_route(err):
                        route_index += 1
                        continue
                    switch_client = True
                    break

                try:
                    status = await self._async_probe_stream(info)
                except (ClientError, TimeoutError, OSError) as err:
                    last_error = err
                    route_index += 1
                    continue

                if status in _STREAM_PROBE_OK:
                    return info

                last_error = RuntimeError(
                    f"YouTube media URL rejected the probe with HTTP {status}"
                )
                if status in _STREAM_PROBE_SWITCH_CLIENT:
                    # Authentication/rate-limit failures are normally tied to
                    # the client profile rather than one quality tier.
                    switch_client = True
                    break

                # 403/404/410 and transient upstream failures can be tied to a
                # specific GoogleVideo URL, so keep stepping down in quality.
                route_index += 1

            if switch_client:
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError("No playable YouTube media stream is available")

    async def _async_probe_stream(self, info: StreamInfo) -> int:
        """Probe one byte from a direct media URL without blocking HA startup."""
        headers = {
            key: value
            for key, value in info.http_headers.items()
            if key.casefold()
            not in {
                "authorization",
                "proxy-authorization",
                "host",
                "content-length",
                "connection",
                "transfer-encoding",
            }
        }
        headers["Range"] = "bytes=0-0"
        client = async_get_clientsession(self.hass)
        response = await client.get(
            info.url,
            headers=headers,
            allow_redirects=True,
            timeout=_STREAM_PROBE_TIMEOUT,
            auto_decompress=False,
        )
        try:
            return response.status
        finally:
            response.release()

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
        player_clients: tuple[str, ...] | None = None,
    ) -> StreamSession | None:
        """Refresh an expired/rejected upstream URL, optionally changing route/client."""
        session = self.get_stream_session(token)
        if session is None:
            return None

        current = session.info.route_index
        if advance_route:
            # Cycle through every *other* route exactly once, then stop. The
            # previous implementation only considered routes after the current
            # index and could miss a known-good earlier route after a refresh.
            indexes = (
                tuple(range(current + 1, len(STREAM_FORMAT_SELECTORS)))
                + tuple(range(0, current))
            )
            if not indexes:
                indexes = (current,)
        else:
            indexes = (current,)

        info = await self.async_resolve_stream(
            session.source_url,
            indexes,
            player_clients=(
                session.info.player_clients
                if player_clients is None
                else player_clients
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
                metadata = self._local_audio_metadata(
                    path, relative_posix, stat.st_mtime_ns, stat.st_size
                )
                items.append(
                    {
                        "id": relative_posix,
                        "title": metadata["title"],
                        "artist": metadata["artist"],
                        "duration": metadata["duration"],
                        "file_format": path.suffix.lower().lstrip("."),
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

        # Drop metadata for files that no longer exist so repeated rescans stay bounded.
        live_ids = {str(item["id"]) for item in items}
        self._library_metadata_cache = {
            key: value
            for key, value in self._library_metadata_cache.items()
            if key in live_ids
        }

        # Downloads are usually what the user wants first; newest files therefore
        # appear at the top, with filename as a deterministic tie-breaker.
        items.sort(
            key=lambda item: (
                -float(item["modified"]),
                str(item["filename"]).casefold(),
            )
        )
        return items


    def _local_audio_metadata(
        self, path: Path, relative_posix: str, mtime_ns: int, size: int
    ) -> dict[str, Any]:
        """Read tags/duration cheaply and cache them until the file changes.

        Mutagen reads container headers/tags instead of decoding the whole audio file.
        This method only runs inside Home Assistant's executor as part of a user-driven
        library scan, never on the event loop or during integration startup.
        """
        cached = self._library_metadata_cache.get(relative_posix)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]

        fallback_artist, fallback_title = _artist_title_from_stem(path.stem)
        title = fallback_title
        artist = fallback_artist
        duration: float | None = None

        try:
            from mutagen import File as MutagenFile

            audio = MutagenFile(path, easy=True)
            if audio is not None:
                info = getattr(audio, "info", None)
                length = getattr(info, "length", None)
                if isinstance(length, (int, float)) and length >= 0:
                    duration = round(float(length), 3)

                tags = getattr(audio, "tags", None)
                if tags:
                    tag_title = _first_tag_value(tags, "title")
                    tag_artist = _first_tag_value(tags, "artist")
                    if tag_title:
                        title = tag_title
                    if tag_artist:
                        artist = tag_artist
        except Exception as err:  # noqa: BLE001 - corrupt tags must not hide the file
            _LOGGER.debug("Unable to read audio metadata for %s: %s", path, err)

        metadata = {
            "title": title or path.stem,
            "artist": artist,
            "duration": duration,
        }
        self._library_metadata_cache[relative_posix] = (mtime_ns, size, metadata)
        return metadata

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



def _first_tag_value(tags: Any, key: str) -> str | None:
    """Return one normalized easy-tag value without assuming a concrete mapping."""
    try:
        value = tags.get(key)
    except (AttributeError, KeyError, TypeError):
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _artist_title_from_stem(stem: str) -> tuple[str | None, str]:
    """Best-effort fallback for common ``Artist - Title [video_id]`` names."""
    value = re.sub(r"\s*\[[A-Za-z0-9_-]{6,}\]\s*$", "", stem).strip()
    if " - " in value:
        artist, title = value.split(" - ", 1)
        if artist.strip() and title.strip():
            return artist.strip(), title.strip()
    return None, value


def _format_from_mime(mime_type: str) -> str:
    """Map a media MIME type to a compact format label for favorites."""
    base = mime_type.split(";", 1)[0].strip().casefold()
    return {
        "audio/mp4": "m4a",
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/flac": "flac",
        "audio/wav": "wav",
        "audio/aac": "aac",
        "video/mp4": "mp4",
        "video/webm": "webm",
    }.get(base, "audio")

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


def _page_reload_error(error: Exception) -> bool:
    """Return whether YouTube rejected the player response as transient reload."""
    return "the page needs to be reloaded" in str(error).casefold()


def _no_player_clients_error(error: Exception) -> bool:
    """Return whether yt-dlp ended up with an empty YouTube client set."""
    return "no player clients have been requested" in str(error).casefold()


def _requested_format_unavailable(error: Exception) -> bool:
    """Return whether yt-dlp says the requested quality tier does not exist."""
    message = str(error).casefold()
    return (
        "requested format is not available" in message
        or "no video formats found" in message
        or "only images are available" in message
    )


def _retry_lower_stream_route(error: Exception) -> bool:
    """Return whether a different direct media URL may recover extraction."""
    message = str(error).casefold()
    return (
        "http error 403" in message
        or "403: forbidden" in message
        or "http error 404" in message
        or "http error 410" in message
        or "http error 416" in message
        or "unable to download video data" in message
        or "fragment" in message
        or "timed out" in message
        or "timeout" in message
        or "connection reset" in message
        or "remote end closed connection" in message
    )


def _stream_mime_type(ext: str, acodec: str, vcodec: str = "") -> str:
    """Return an accurate speaker-friendly MIME type for a selected format."""
    has_video = bool(vcodec and vcodec not in {"none", "null"})
    if has_video and ext == "mp4":
        return "video/mp4"
    if has_video and ext == "webm":
        return "video/webm"
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
