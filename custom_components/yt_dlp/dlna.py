"""Optional DLNA compatibility layer for YouTube-DLP playback.

This module is deliberately isolated from the protected download/playback core.
It materializes a complete MP3 for DLNA DMR targets and serves that file with
DLNA-friendly HTTP headers so Home Assistant/async_upnp_client can build stable
DIDL-Lite protocolInfo metadata.
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
from homeassistant.helpers.importlib import async_import_module

if TYPE_CHECKING:
    from .playback import PlaybackManager, StreamSession

from .helpers import detect_javascript_runtime, youtube_dl_class
from .js_runtime import async_ensure_javascript_runtime

_LOGGER = logging.getLogger(__name__)

DLNA_MEDIA_URL_PREFIX = "/api/yt_dlp/dlna"
DLNA_MIME_TYPE = "audio/mpeg"
DLNA_CONTENT_FEATURES = (
    "DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01D00000000000000000000000000000"
)
_DLNA_CACHE_TTL_SECONDS = 8 * 60 * 60
_MAX_DLNA_CACHE_ITEMS = 24
_REMOTE_TIMEOUT = ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=90)
_RETRYABLE_UPSTREAM_STATUSES = frozenset({401, 403, 404, 410, 416, 429})
_YOUTUBE_DOWNLOAD_CLIENT_PROFILES: tuple[tuple[str, ...], ...] = (
    ("default", "web_embedded"),
    ("android_vr",),
    ("web_embedded",),
    ("web_safari",),
)
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
    """One complete DLNA-compatible cache file."""

    token: str
    source_key: str
    path: Path
    created_at: float
    last_access: float


class DlnaPlaybackManager:
    """Prepare and cache complete MP3 files for strict DLNA renderers."""

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
        """Materialize one remote playback session as a complete MP3."""
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
                await self._async_transcode_remote(playback, stream_token, temp_path)
                await self.hass.async_add_executor_job(
                    _replace_file, temp_path, final_path
                )
            except asyncio.CancelledError:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise
            except Exception:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise

            size = await self.hass.async_add_executor_job(_valid_media_size, final_path)
            if size <= 0:
                await self.hass.async_add_executor_job(_safe_unlink, final_path)
                raise RuntimeError("DLNA transcode produced an empty MP3")

            now = time.monotonic()
            item = DlnaCacheItem(token, source_key, final_path, now, now)
            self._items_by_source[source_key] = item
            self._items_by_token[token] = item
            await self._async_prune_locked()
            _LOGGER.info("Prepared DLNA MP3 cache (%d bytes)", size)
            return self._url_for(token)

    async def async_prepare_local(self, path: Path) -> str:
        """Materialize one local library item as a DLNA-compatible MP3."""
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
                if path.suffix.lower() == ".mp3":
                    await self.hass.async_add_executor_job(
                        _copy_file, path, temp_path
                    )
                else:
                    ffmpeg = await self._async_resolve_ffmpeg()
                    await self._run_ffmpeg_file(ffmpeg, path, temp_path)
                await self.hass.async_add_executor_job(
                    _replace_file, temp_path, final_path
                )
            except asyncio.CancelledError:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise
            except Exception:
                await self.hass.async_add_executor_job(_safe_unlink, temp_path)
                raise

            size = await self.hass.async_add_executor_job(_valid_media_size, final_path)
            if size <= 0:
                await self.hass.async_add_executor_job(_safe_unlink, final_path)
                raise RuntimeError("DLNA transcode produced an empty MP3")

            now = time.monotonic()
            item = DlnaCacheItem(token, source_key, final_path, now, now)
            self._items_by_source[source_key] = item
            self._items_by_token[token] = item
            await self._async_prune_locked()
            return self._url_for(token)

    def get_file(self, token: str) -> Path | None:
        """Return a prepared MP3 path for an HTTP request."""
        item = self._items_by_token.get(token)
        if item is None:
            return None
        if time.monotonic() - item.last_access > _DLNA_CACHE_TTL_SECONDS:
            return None
        if not item.path.is_file():
            return None
        item.last_access = time.monotonic()
        return item.path

    async def async_shutdown(self) -> None:
        """Drop cache state and remove temporary transcodes."""
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

        # Mirror the relay's bounded refresh matrix.  A one-byte probe may pass
        # while a later full request gets 403, so each rejected URL is refreshed
        # through the same known-good client/quality routes used by normal Play.
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

        original = playback.get_stream_session(stream_token)
        source_url = original.source_url if original is not None else None

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
                except Exception as err:  # noqa: BLE001 - bounded fallback path
                    last_error = err
                    continue
            if session is None:
                continue

            try:
                await self._run_ffmpeg_remote(ffmpeg, session, output_path)
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - try next safe source route
                last_error = err
                await self.hass.async_add_executor_job(_safe_unlink, output_path)

        # If every direct GoogleVideo URL is rejected, use yt-dlp's downloader
        # as the final materialization path.  This deliberately re-extracts the
        # original YouTube page and uses check_formats=selected, the same robust
        # behavior that already makes the integration's Download service work.
        if source_url:
            try:
                await self._async_materialize_with_ytdlp(
                    source_url, ffmpeg, output_path
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - surface both paths below
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
        # YouTube's media CDN increasingly accepts a byte-range fetch while the
        # equivalent un-ranged full GET returns 403.  The normal speaker relay
        # already benefits from renderer Range requests; DLNA transcode must do
        # the same explicitly because ffmpeg is fed through this pipe.
        headers["Range"] = "bytes=0-"
        headers.setdefault("Accept-Encoding", "identity")
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
            if response.status in _RETRYABLE_UPSTREAM_STATUSES:
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

            stderr = (
                await process.stderr.read()
            ).decode("utf-8", errors="replace").strip()
            return_code = await process.wait()
            if return_code != 0:
                raise RuntimeError(stderr or f"ffmpeg exited with status {return_code}")
        except asyncio.CancelledError:
            raise
        except (ClientError, TimeoutError, OSError, BrokenPipeError, ConnectionResetError) as err:
            raise RuntimeError(f"DLNA transcode input failed: {err}") from err
        finally:
            if response is not None:
                response.release()
            if process.returncode is None:
                await _async_terminate_process(process)

    async def _async_materialize_with_ytdlp(
        self, source_url: str, ffmpeg: str, output_path: Path
    ) -> None:
        """Download a fresh source with yt-dlp, then transcode it to MP3.

        This is a DLNA-only fallback for YouTube CDN URLs that probe correctly
        but later reject every direct transfer with HTTP 403.  It is lazy and
        runs only after the direct path has failed, so regular Play/Download and
        Home Assistant startup remain untouched.
        """
        staging_dir = output_path.parent / f".{output_path.stem}.yt-dlp"
        await self.hass.async_add_executor_job(_safe_rmtree, staging_dir)
        await self.hass.async_add_executor_job(staging_dir.mkdir, 0o700, True, True)
        try:
            # The direct relay normally reaches this fallback only after normal
            # playback has already prepared the JS runtime. Ensure it here too
            # so DLNA remains correct when invoked through a future/direct path.
            await async_ensure_javascript_runtime(self.hass)
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            source_path = await self.hass.async_add_executor_job(
                _download_dlna_source_sync,
                youtube_dl_cls,
                source_url,
                staging_dir,
                ffmpeg,
            )
            await self._run_ffmpeg_file(ffmpeg, source_path, output_path)
        finally:
            await self.hass.async_add_executor_job(_safe_rmtree, staging_dir)

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
        try:
            stderr = (
                await process.stderr.read()
            ).decode("utf-8", errors="replace").strip()
            return_code = await process.wait()
            if return_code != 0:
                raise RuntimeError(stderr or f"ffmpeg exited with status {return_code}")
        finally:
            if process.returncode is None:
                await _async_terminate_process(process)

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
        if item is None or not item.path.is_file():
            return None
        item.last_access = time.monotonic()
        return item

    async def _async_prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            item
            for item in self._items_by_token.values()
            if now - item.last_access > _DLNA_CACHE_TTL_SECONDS or not item.path.is_file()
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
        # The query string is intentional. Home Assistant's
        # async_process_play_media_url does not append a long authSig when a
        # query is already present. The random token is the short-lived
        # capability secret for this otherwise unauthenticated DLNA endpoint.
        return f"{DLNA_MEDIA_URL_PREFIX}/{token}.mp3?dlna=1"


class YoutubeDlpDlnaView(HomeAssistantView):
    """Serve complete MP3s with range support and DLNA protocol headers."""

    url = f"{DLNA_MEDIA_URL_PREFIX}/{{token}}.mp3"
    name = "api:yt_dlp:dlna"
    # DLNA renderers cannot send HA authentication headers. The URL contains a
    # high-entropy, short-lived capability token and cannot be enumerated.
    # Keeping this view unauthenticated also avoids authSig URLs that exceed
    # limits on a number of embedded DLNA renderers.
    requires_auth = False

    async def _path(self, hass: HomeAssistant, token: str) -> Path:
        from .dlna_runtime import get_dlna_manager

        manager = get_dlna_manager(hass)
        path = manager.get_file(token)
        if path is None:
            raise web.HTTPNotFound(text="DLNA playback cache has expired")
        return path

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Content-Type": DLNA_MIME_TYPE,
            "Accept-Ranges": "bytes",
            "ContentFeatures.dlna.org": DLNA_CONTENT_FEATURES,
            "transferMode.dlna.org": "Streaming",
            "Content-Disposition": 'inline; filename="stream.mp3"',
        }

    async def head(self, request: web.Request, token: str) -> web.FileResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, token)
        return web.FileResponse(path, headers=self._headers())

    async def get(self, request: web.Request, token: str) -> web.FileResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, token)
        return web.FileResponse(path, headers=self._headers())


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
    candidate = Path(hint)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(hint) or shutil.which("ffmpeg")


def _valid_media_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _download_dlna_source_sync(
    youtube_dl_cls, source_url: str, staging_dir: Path, ffmpeg: str
) -> Path:
    """Materialize one fresh downloadable audio source with yt-dlp."""
    runtime = detect_javascript_runtime()
    last_error: Exception | None = None

    for player_clients in _YOUTUBE_DOWNLOAD_CLIENT_PROFILES:
        # A fresh directory per profile avoids a failed partial file being
        # mistaken for a successful source by the next retry.
        profile_dir = staging_dir / ("-".join(player_clients) or "default")
        _safe_rmtree(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        opts: dict[str, object] = {
            "paths": {"home": str(profile_dir), "temp": str(profile_dir)},
            "outtmpl": {"default": "source.%(ext)s"},
            "format": "bestaudio/best",
            "noplaylist": True,
            "overwrites": True,
            "continuedl": True,
            "nopart": False,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "check_formats": "selected",
            "ffmpeg_location": ffmpeg,
            "extractor_args": {
                "youtube": {"player_client": list(player_clients)}
            },
        }
        if runtime:
            name, path = runtime
            opts["js_runtimes"] = {name: {"path": path}}

        try:
            with youtube_dl_cls(opts) as ydl:
                info = ydl.extract_info(source_url, download=True)
        except Exception as err:  # yt-dlp exposes multiple DownloadError variants
            last_error = err
            continue

        candidates: list[Path] = []
        if isinstance(info, dict):
            requested = info.get("requested_downloads")
            if isinstance(requested, list):
                for item in requested:
                    if isinstance(item, dict) and item.get("filepath"):
                        candidates.append(Path(str(item["filepath"])))
            filename = info.get("_filename")
            if filename:
                candidates.append(Path(str(filename)))

        candidates.extend(
            path
            for path in profile_dir.iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        )
        valid = [path for path in candidates if path.is_file() and path.stat().st_size > 0]
        if valid:
            return max(valid, key=lambda path: path.stat().st_size)

        last_error = RuntimeError("yt-dlp completed without a materialized source file")

    if last_error is not None:
        raise RuntimeError(f"yt-dlp DLNA source fallback failed: {last_error}") from last_error
    raise RuntimeError("yt-dlp DLNA source fallback failed")


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
