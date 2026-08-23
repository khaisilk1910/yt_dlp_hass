"""DLNA-specific compatibility playback for YouTube-DLP.

Home Assistant's DLNA DMR integration resolves media-source URIs to HTTP URLs,
probes those URLs, builds DIDL-Lite metadata, then asks the renderer to fetch the
media itself.  Many audio renderers advertise a much narrower set of accepted
containers/codecs than Cast devices.  This module prepares a stable MP3 file for
DLNA targets while leaving the normal Cast/media_player path unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from .playback import PlaybackManager, StreamSession

_LOGGER = logging.getLogger(__name__)

DLNA_MEDIA_URL_PREFIX = "/api/yt_dlp/dlna"
DLNA_MIME_TYPE = "audio/mpeg"
_DLNA_CACHE_TTL_SECONDS = 8 * 60 * 60
_MAX_DLNA_CACHE_ITEMS = 24
_REMOTE_TIMEOUT = ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=60)
_UPSTREAM_RETRY_STATUSES = frozenset({401, 403, 404, 410, 416, 429})
_HOP_BY_HOP_HEADERS = frozenset(
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


@dataclass(slots=True)
class DlnaCacheItem:
    """One transcoded DLNA-compatible cache file."""

    token: str
    source_key: str
    path: Path
    created_at: float
    last_access: float


class DlnaPlaybackManager:
    """Prepare complete MP3 files for strict DLNA renderers."""

    def __init__(self, hass: HomeAssistant, ffmpeg_hint: str | None) -> None:
        self.hass = hass
        self._ffmpeg_hint = (ffmpeg_hint or "ffmpeg").strip() or "ffmpeg"
        self._ffmpeg_binary: str | None = None
        self._cache_dir = Path(tempfile.gettempdir()) / (
            f"yt_dlp_dlna_{secrets.token_hex(8)}"
        )
        self._lock = asyncio.Lock()
        self._items_by_source: dict[str, DlnaCacheItem] = {}
        self._items_by_token: dict[str, DlnaCacheItem] = {}

    async def async_prepare_remote(
        self, playback: PlaybackManager, stream_token: str
    ) -> str:
        """Transcode one live yt-dlp stream session to a stable MP3 file."""
        session = playback.get_stream_session(stream_token)
        if session is None:
            raise RuntimeError("Playback stream has expired")

        source_key = f"remote:{session.source_url}"
        async with self._lock:
            await self._async_prune_locked()
            if cached := self._get_cached_locked(source_key):
                return self._url_for(cached.token)

            await self._async_ensure_cache_dir()
            token = self._new_token()
            final_path = self._cache_dir / f"{token}.mp3"
            temp_path = self._cache_dir / f".{token}.part.mp3"
            try:
                await self._async_transcode_remote(
                    playback, stream_token, temp_path
                )
                await self.hass.async_add_executor_job(
                    _replace_file, temp_path, final_path
                )
            except Exception:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise

            now = time.monotonic()
            item = DlnaCacheItem(token, source_key, final_path, now, now)
            self._items_by_source[source_key] = item
            self._items_by_token[token] = item
            await self._async_prune_locked()
            _LOGGER.debug("Prepared DLNA MP3 cache for remote source: %s", source_key)
            return self._url_for(token)

    async def async_prepare_local(self, path: Path) -> str:
        """Transcode a local library file to a stable DLNA-compatible MP3."""
        stat = await self.hass.async_add_executor_job(path.stat)
        source_key = f"local:{path}:{stat.st_mtime_ns}:{stat.st_size}"

        async with self._lock:
            await self._async_prune_locked()
            if cached := self._get_cached_locked(source_key):
                return self._url_for(cached.token)

            await self._async_ensure_cache_dir()
            token = self._new_token()
            final_path = self._cache_dir / f"{token}.mp3"
            temp_path = self._cache_dir / f".{token}.part.mp3"
            try:
                ffmpeg = await self._async_resolve_ffmpeg()
                await self._run_ffmpeg_file(ffmpeg, path, temp_path)
                await self.hass.async_add_executor_job(
                    _replace_file, temp_path, final_path
                )
            except Exception:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise

            now = time.monotonic()
            item = DlnaCacheItem(token, source_key, final_path, now, now)
            self._items_by_source[source_key] = item
            self._items_by_token[token] = item
            await self._async_prune_locked()
            _LOGGER.debug("Prepared DLNA MP3 cache for local source: %s", path)
            return self._url_for(token)

    def get_file(self, token: str) -> Path | None:
        """Return a prepared MP3 path for an HTTP request."""
        item = self._items_by_token.get(token)
        if item is None:
            return None
        if time.monotonic() - item.last_access > _DLNA_CACHE_TTL_SECONDS:
            return None
        item.last_access = time.monotonic()
        return item.path

    async def async_shutdown(self) -> None:
        """Drop in-memory cache state and remove temporary transcodes."""
        async with self._lock:
            self._items_by_source.clear()
            self._items_by_token.clear()
            cache_dir = self._cache_dir
        await self.hass.async_add_executor_job(_safe_rmtree, cache_dir)

    async def _async_transcode_remote(
        self, playback: PlaybackManager, stream_token: str, output_path: Path
    ) -> None:
        ffmpeg = await self._async_resolve_ffmpeg()
        last_error: Exception | None = None
        attempts: tuple[tuple[bool | None, tuple[str, ...] | None], ...] = (
            (None, None),
            (False, None),
            (True, None),
            (False, ("android_vr",)),
            (True, ("android_vr",)),
            (False, ("web_embedded",)),
            (True, ("web_embedded",)),
            (False, ("web_safari",)),
            (True, ("web_safari",)),
        )

        for advance_route, player_clients in attempts:
            if advance_route is None:
                session = playback.get_stream_session(stream_token)
            else:
                try:
                    session = await playback.async_refresh_stream(
                        stream_token,
                        advance_route=advance_route,
                        player_clients=player_clients,
                    )
                except Exception as err:  # noqa: BLE001 - bounded compatibility fallbacks
                    last_error = err
                    continue
            if session is None:
                continue

            try:
                await self._run_ffmpeg_remote(ffmpeg, session, output_path)
                return
            except Exception as err:  # noqa: BLE001 - try next safe upstream route
                last_error = err
                await self.hass.async_add_executor_job(_safe_unlink, output_path)

        if last_error is not None:
            raise RuntimeError(f"Unable to prepare DLNA MP3: {last_error}") from last_error
        raise RuntimeError("Unable to prepare DLNA MP3")

    async def _run_ffmpeg_remote(
        self, ffmpeg: str, session: StreamSession, output_path: Path
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0?",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-id3v2_version",
            "3",
            "-f",
            "mp3",
            "-y",
            str(output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None

        headers = {
            key: value
            for key, value in session.info.http_headers.items()
            if key.casefold() not in _HOP_BY_HOP_HEADERS
            and key.casefold() not in {"authorization", "proxy-authorization"}
        }
        client = async_get_clientsession(self.hass)
        response = None
        try:
            response = await client.get(
                session.info.url,
                headers=headers,
                allow_redirects=True,
                timeout=_REMOTE_TIMEOUT,
                auto_decompress=False,
            )
            if response.status in _UPSTREAM_RETRY_STATUSES:
                raise RuntimeError(
                    f"upstream media rejected DLNA transcode with HTTP {response.status}"
                )
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"upstream media returned HTTP {response.status} during DLNA transcode"
                )

            async for chunk in response.content.iter_chunked(128 * 1024):
                process.stdin.write(chunk)
                await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

            stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            return_code = await process.wait()
            if return_code != 0:
                raise RuntimeError(stderr or f"ffmpeg exited with status {return_code}")
        except (ClientError, TimeoutError, OSError, BrokenPipeError, ConnectionResetError) as err:
            raise RuntimeError(f"DLNA transcode input failed: {err}") from err
        finally:
            if response is not None:
                response.release()
            if process.returncode is None:
                await _async_terminate_process(process)

    async def _run_ffmpeg_file(
        self, ffmpeg: str, source_path: Path, output_path: Path
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source_path),
            "-map",
            "0:a:0?",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-id3v2_version",
            "3",
            "-f",
            "mp3",
            "-y",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stderr is not None
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError(stderr or f"ffmpeg exited with status {return_code}")

    async def _async_resolve_ffmpeg(self) -> str:
        if self._ffmpeg_binary:
            return self._ffmpeg_binary
        resolved = await self.hass.async_add_executor_job(
            _resolve_ffmpeg_binary, self._ffmpeg_hint
        )
        if not resolved:
            raise RuntimeError("FFmpeg is unavailable for DLNA compatibility playback")
        self._ffmpeg_binary = resolved
        return resolved

    async def _async_ensure_cache_dir(self) -> None:
        await self.hass.async_add_executor_job(
            self._cache_dir.mkdir, 0o700, True, True
        )

    def _get_cached_locked(self, source_key: str) -> DlnaCacheItem | None:
        item = self._items_by_source.get(source_key)
        if item is None:
            return None
        item.last_access = time.monotonic()
        return item

    async def _async_prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            item
            for item in self._items_by_token.values()
            if now - item.last_access > _DLNA_CACHE_TTL_SECONDS
        ]
        remaining = len(self._items_by_token) - len(expired)
        if remaining > _MAX_DLNA_CACHE_ITEMS:
            keep = {
                item.token
                for item in sorted(
                    self._items_by_token.values(), key=lambda item: item.last_access
                )[-_MAX_DLNA_CACHE_ITEMS:]
            }
            expired.extend(
                item
                for item in self._items_by_token.values()
                if item.token not in keep and item not in expired
            )

        for item in expired:
            self._items_by_token.pop(item.token, None)
            if self._items_by_source.get(item.source_key) is item:
                self._items_by_source.pop(item.source_key, None)
        if expired:
            await asyncio.gather(
                *(
                    self.hass.async_add_executor_job(_safe_unlink, item.path)
                    for item in expired
                )
            )

    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(24)
            if token not in self._items_by_token:
                return token

    @staticmethod
    def _url_for(token: str) -> str:
        # No query string: Home Assistant's DLNA integration will convert this
        # relative URL to an absolute LAN URL and attach authSig for this view.
        return f"{DLNA_MEDIA_URL_PREFIX}/{token}.mp3"


class YoutubeDlpDlnaView(HomeAssistantView):
    """Serve complete transcoded MP3s with normal aiohttp range semantics."""

    url = f"{DLNA_MEDIA_URL_PREFIX}/{{token}}.mp3"
    name = "api:yt_dlp:dlna"
    requires_auth = True

    async def _path(self, hass: HomeAssistant, token: str) -> Path:
        from .dlna_runtime import get_dlna_manager

        manager = get_dlna_manager(hass)
        path = manager.get_file(token)
        if path is None:
            raise web.HTTPNotFound(text="DLNA playback cache has expired")
        return path

    async def head(self, request: web.Request, token: str) -> web.FileResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, token)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def get(self, request: web.Request, token: str) -> web.FileResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, token)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _async_terminate_process(process: asyncio.subprocess.Process) -> None:
    """Stop an ffmpeg child process without leaving zombies behind."""
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        await process.wait()


def _resolve_ffmpeg_binary(hint: str) -> str | None:
    """Resolve HA's ffmpeg hint to an executable path off the event loop."""
    if Path(hint).is_file():
        return str(Path(hint).resolve())
    return shutil.which(hint) or shutil.which("ffmpeg")


def _replace_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source.replace(destination)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
