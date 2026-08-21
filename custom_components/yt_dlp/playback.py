"""Direct playback and local media library support for YouTube-DLP.

This module is intentionally separate from manager.py so download behavior stays
unchanged. All yt-dlp extraction and filesystem scanning run in HA executors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.importlib import async_import_module

from .helpers import detect_javascript_runtime, normalize_download_directory

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


@dataclass(slots=True, frozen=True)
class StreamInfo:
    """Resolved direct stream metadata."""

    url: str
    title: str
    thumbnail: str | None
    artist: str | None
    duration: int | float | None
    mime_type: str
    webpage_url: str

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe response data."""
        return {
            "url": self.webpage_url,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "artist": self.artist,
            "duration": self.duration,
            "mime_type": self.mime_type,
        }


class PlaybackManager:
    """Resolve remote audio and scan the configured local music library."""

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

    async def async_resolve_stream(self, url: str) -> StreamInfo:
        """Resolve a playable YouTube/yt-dlp audio URL without downloading it."""
        async with self._stream_lock:
            await async_import_module(self.hass, "yt_dlp")
            return await self.hass.async_add_executor_job(
                self._resolve_stream_sync, url
            )

    def _resolve_stream_sync(self, url: str) -> StreamInfo:
        """Blocking yt-dlp metadata extraction. Executor thread only."""
        from yt_dlp.YoutubeDL import YoutubeDL
        from yt_dlp.utils import DownloadError

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            # Prefer AAC/M4A for broad speaker compatibility, then independent
            # audio routes, then a progressive MP4 as a last compatibility route.
            "format": (
                "ba[ext=m4a]/ba[acodec^=mp4a]/"
                "ba[ext=webm]/ba/b[ext=mp4]/b"
            ),
            "socket_timeout": 20,
            "retries": 3,
            "extractor_retries": 2,
        }
        if js_runtimes := self._js_runtime_options():
            opts["js_runtimes"] = js_runtimes

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not isinstance(info, dict):
            raise DownloadError("yt-dlp did not return media information")

        stream_url = info.get("url")
        if not isinstance(stream_url, str) or not stream_url.startswith(
            ("http://", "https://")
        ):
            raise DownloadError("yt-dlp did not return a playable direct media URL")

        ext = str(info.get("ext") or "").lower()
        acodec = str(info.get("acodec") or "").lower()
        mime_type = _stream_mime_type(ext, acodec)
        thumbnail = _best_thumbnail(info)
        title = str(info.get("title") or info.get("fulltitle") or "YouTube audio")
        artist_value = info.get("artist") or info.get("channel") or info.get("uploader")
        artist = str(artist_value) if artist_value else None
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)

        return StreamInfo(
            url=stream_url,
            title=title,
            thumbnail=thumbnail,
            artist=artist,
            duration=info.get("duration"),
            mime_type=mime_type,
            webpage_url=webpage_url,
        )

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
