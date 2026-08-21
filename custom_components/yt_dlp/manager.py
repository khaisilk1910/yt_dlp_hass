"""yt-dlp worker and job manager.

All yt-dlp work is synchronous/blocking and must only run in Home Assistant's
executor. State updates are marshalled back to the event-loop thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.importlib import async_import_module

from .const import (
    CACHE_DIR_NAME,
    DEFAULT_FILENAME_TEMPLATE,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_CONCURRENT_SEARCHES,
    MAX_RETAINED_JOBS,
    MEDIA_TYPE_AUDIO,
    STATE_DOWNLOADER,
    TEMP_DIR_NAME,
)
from .helpers import detect_javascript_runtime, ensure_writable_directory

_LOGGER = logging.getLogger(__name__)
_JS_RUNTIME_UNSET = object()

# YouTube changes player-client/PO-token enforcement frequently. Always let the
# installed yt-dlp choose its current upstream defaults first. Only isolate a
# client after an actual HTTP 403, then finally retry upstream defaults over
# IPv4. This avoids freezing old player-client assumptions into the integration.
_PRIMARY_YOUTUBE_CLIENTS: tuple[str, ...] | None = None
_FALLBACK_YOUTUBE_CLIENTS = (("web_embedded",), ("default", "web_embedded"))


@dataclass(slots=True)
class DownloadRequest:
    """Validated download settings for one job."""

    url: str
    media_type: str
    video_quality: str
    video_format: str
    audio_format: str
    audio_quality: str
    overwrite: bool


@dataclass(slots=True)
class DownloadJob:
    """Runtime information for one download."""

    job_id: str
    request: DownloadRequest
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[Any] | None = None
    status: str = "queued"
    filename: str | None = None
    title: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    eta: float | None = None
    final_files: list[str] = field(default_factory=list)
    error: str | None = None


class YoutubeDlpManager:
    """Own download/search workers for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        download_path: str,
        ffmpeg_path: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.download_path = download_path
        self.ffmpeg_path = ffmpeg_path
        self.jobs: dict[str, DownloadJob] = {}
        self._stopping = False
        self._javascript_runtime: tuple[str, str] | None | object = _JS_RUNTIME_UNSET
        self._javascript_runtime_lock = threading.Lock()
        self._download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    def _js_runtime_options(self) -> dict[str, dict[str, str]] | None:
        """Lazily detect a yt-dlp JavaScript runtime inside a worker thread."""
        if self._javascript_runtime is _JS_RUNTIME_UNSET:
            with self._javascript_runtime_lock:
                if self._javascript_runtime is _JS_RUNTIME_UNSET:
                    self._javascript_runtime = detect_javascript_runtime()
                    if self._javascript_runtime:
                        _LOGGER.info(
                            "YouTube-DLP detected JavaScript runtime: %s",
                            self._javascript_runtime[0],
                        )
                    else:
                        _LOGGER.debug("No external yt-dlp JavaScript runtime detected")

        runtime = self._javascript_runtime
        if not isinstance(runtime, tuple):
            return None
        name, path = runtime
        return {name: {"path": path}}

    async def async_start_download(self, request: DownloadRequest) -> DownloadJob:
        """Start a download in a background task and return its job immediately."""
        if self._stopping:
            raise RuntimeError("Integration is unloading")

        self._prune_finished_jobs()
        job = DownloadJob(job_id=uuid4().hex, request=request)
        self.jobs[job.job_id] = job
        self.async_publish_state()

        job.task = self.entry.async_create_background_task(
            self.hass,
            self._async_run_download(job),
            f"{STATE_DOWNLOADER}_{job.job_id}",
        )
        return job

    async def async_wait_for_job(self, job: DownloadJob) -> dict[str, Any]:
        """Wait for a job without blocking Home Assistant's event loop."""
        if job.task is not None:
            await job.task
        return self.job_response(job)

    async def _async_run_download(self, job: DownloadJob) -> None:
        """Run one blocking yt-dlp operation in the executor."""
        try:
            async with self._download_semaphore:
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    return
                # yt-dlp is intentionally imported lazily so Home Assistant startup
                # does not pay its import cost. Use HA's import helper so the first
                # concurrent search/download cannot race CPython's import machinery.
                await async_import_module(self.hass, "yt_dlp")
                result = await self.hass.async_add_executor_job(self._download_sync, job)
        except asyncio.CancelledError:
            job.cancel_event.set()
            job.status = "cancelled"
            self.async_publish_state()
            raise
        except Exception as err:  # noqa: BLE001 - worker errors are surfaced in state/log
            job.status = "error"
            job.error = str(err)
            _LOGGER.exception("yt-dlp download job %s failed", job.job_id)
        else:
            job.status = "completed"
            job.final_files = result
            job.filename = os.path.basename(result[0]) if result else job.filename
            job.error = None
        finally:
            # Temp cleanup is owned by the executor worker itself. This avoids
            # deleting fragments underneath a worker if Home Assistant cancels
            # the async wrapper during config-entry unload.
            self._prune_finished_jobs()
            self.async_publish_state()

    def _download_sync(self, job: DownloadJob) -> list[str]:
        """Run one blocking download and always clean its private temp dir."""
        # Do not validate storage during config-entry setup. A NAS/mount may be
        # temporarily offline at HA startup; validate it only when a user asks
        # for an actual download.
        ensure_writable_directory(self.download_path)
        job_temp = os.path.join(self.download_path, TEMP_DIR_NAME, job.job_id)
        try:
            return self._download_sync_worker(job, job_temp)
        finally:
            # This executes in the same executor thread after yt-dlp has really
            # stopped, so cleanup cannot race an in-flight downloader.
            self._cleanup_job_temp(job.job_id)

    def _download_sync_worker(self, job: DownloadJob, job_temp: str) -> list[str]:
        """Blocking yt-dlp worker implementation. Executor thread only."""
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadCancelled, DownloadError

        request = job.request

        def progress_hook(data: Mapping[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled(
                    "Download cancelled because the integration is unloading"
                )

            # yt-dlp's progress payload can contain the complete info_dict
            # (formats, thumbnails, manifests, etc.). Do not queue that large
            # object onto Home Assistant's event loop every second.
            info = data.get("info_dict") or {}
            compact = {
                "status": data.get("status"),
                "filename": data.get("filename"),
                "tmpfilename": data.get("tmpfilename"),
                "downloaded_bytes": data.get("downloaded_bytes"),
                "total_bytes": data.get("total_bytes"),
                "total_bytes_estimate": data.get("total_bytes_estimate"),
                "speed": data.get("speed"),
                "eta": data.get("eta"),
                "title": info.get("title") if isinstance(info, Mapping) else None,
                "info_filename": (
                    info.get("filename") if isinstance(info, Mapping) else None
                ),
            }
            self.hass.add_job(self._handle_progress, job.job_id, compact)

        def postprocessor_hook(data: Mapping[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled(
                    "Download cancelled because the integration is unloading"
                )
            self.hass.add_job(
                self._handle_postprocess_progress,
                job.job_id,
                {"status": data.get("status")},
            )

        attempts = (
            (_PRIMARY_YOUTUBE_CLIENTS, False),
            (_FALLBACK_YOUTUBE_CLIENTS[0], False),
            (_FALLBACK_YOUTUBE_CLIENTS[1], True),
        )
        info: Mapping[str, Any] | None = None
        last_error: DownloadError | None = None

        for attempt_index, (youtube_clients, force_ipv4) in enumerate(attempts, start=1):
            if job.cancel_event.is_set():
                raise DownloadCancelled(
                    "Download cancelled because the integration is unloading"
                )

            # A retry must not resume fragments created using another YouTube
            # player client because those signed URLs may no longer be valid.
            shutil.rmtree(job_temp, ignore_errors=True)
            os.makedirs(job_temp, exist_ok=True)

            ydl_opts = self._build_download_options(
                request,
                job_temp,
                progress_hook,
                postprocessor_hook,
                youtube_clients=youtube_clients,
                force_ipv4=force_ipv4,
            )
            self.hass.add_job(self._set_job_status, job.job_id, "extracting")

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(request.url, download=True)
                info = extracted if isinstance(extracted, Mapping) else None
                break
            except DownloadError as err:
                last_error = err
                if "HTTP Error 403" not in str(err) or attempt_index == len(attempts):
                    raise
                _LOGGER.warning(
                    "YouTube returned HTTP 403 for job %s; retrying with player clients %s%s",
                    job.job_id,
                    ",".join(attempts[attempt_index][0]),
                    " over IPv4" if attempts[attempt_index][1] else "",
                )
                time.sleep(1)

        if not info:
            if last_error is not None:
                raise last_error
            raise DownloadError("yt-dlp did not return media information")

        final_files = [
            path for path in self._extract_final_files(info) if os.path.isfile(path)
        ]
        if not final_files:
            final_files = self._find_final_files_by_id(info)
        if not final_files:
            raise DownloadError(
                "Download finished but the final output file could not be determined"
            )
        return final_files

    def _build_download_options(
        self,
        request: DownloadRequest,
        temp_path: str,
        progress_hook: Any,
        postprocessor_hook: Any,
        *,
        youtube_clients: tuple[str, ...] | None = _PRIMARY_YOUTUBE_CLIENTS,
        force_ipv4: bool = False,
    ) -> dict[str, Any]:
        """Build controlled yt-dlp options from service fields."""
        opts: dict[str, Any] = {
            "paths": {
                "home": self.download_path,
                "temp": temp_path,
            },
            "outtmpl": {"default": DEFAULT_FILENAME_TEMPLATE},
            "cachedir": os.path.join(self.download_path, CACHE_DIR_NAME),
            "noplaylist": True,
            "overwrites": request.overwrite,
            "continuedl": True,
            "nopart": False,
            "quiet": True,
            "no_warnings": False,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "progress_delta": 1.0,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "restrictfilenames": False,
            "trim_file_name": 180,
        }

        if youtube_clients is not None:
            opts["extractor_args"] = {
                "youtube": {"player_client": list(youtube_clients)},
            }

        if force_ipv4:
            # Equivalent to yt-dlp --force-ipv4. This is only used as the last
            # 403 fallback because forcing IPv4 globally would hurt IPv6-only
            # installations.
            opts["source_address"] = "0.0.0.0"

        if self.ffmpeg_path:
            opts["ffmpeg_location"] = self.ffmpeg_path

        if js_runtimes := self._js_runtime_options():
            opts["js_runtimes"] = js_runtimes

        if request.media_type == MEDIA_TYPE_AUDIO:
            opts.update(self._audio_options(request))
        else:
            opts.update(self._video_options(request))
        return opts

    @staticmethod
    def _audio_options(request: DownloadRequest) -> dict[str, Any]:
        """Return yt-dlp options for an audio-only final file."""
        quality = "0" if request.audio_quality == "best" else request.audio_quality
        return {
            "format": "bestaudio/best",
            "final_ext": request.audio_format,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": request.audio_format,
                    "preferredquality": quality,
                }
            ],
        }

    @staticmethod
    def _video_options(request: DownloadRequest) -> dict[str, Any]:
        """Return yt-dlp options for a merged video final file."""
        height_filter = (
            ""
            if request.video_quality == "best"
            else f"[height<={request.video_quality}]"
        )

        if request.video_format == "mp4":
            # Select the best native MP4 video up to the requested height.
            # Do not prefer H.264 here: on YouTube that can silently cap a
            # 1440p/2160p request at 1080p even when a higher MP4 stream exists.
            selector = (
                f"bv[ext=mp4]{height_filter}+ba[ext=m4a]/"
                f"b[ext=mp4]{height_filter}"
            )
        elif request.video_format == "webm":
            selector = (
                f"bv[ext=webm]{height_filter}+ba[ext=webm]/"
                f"b[ext=webm]{height_filter}"
            )
        else:
            # Matroska accepts the widest range of YouTube codecs.
            selector = f"bv{height_filter}+ba/b{height_filter}"

        return {
            "format": selector,
            "merge_output_format": request.video_format,
            "final_ext": request.video_format,
            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": request.video_format,
                }
            ],
        }

    async def async_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search YouTube in a bounded executor worker."""
        if self._stopping:
            raise RuntimeError("Integration is unloading")
        async with self._search_semaphore:
            # Keep the heavy optional dependency out of startup while ensuring
            # thread-safe lazy importing on the first action call.
            await async_import_module(self.hass, "yt_dlp")
            return await self.hass.async_add_executor_job(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Blocking YouTube search. Executor thread only."""
        from yt_dlp import YoutubeDL

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "noplaylist": False,
            "lazy_playlist": True,
            "socket_timeout": 20,
            "extractor_retries": 2,
        }
        if js_runtimes := self._js_runtime_options():
            opts["js_runtimes"] = js_runtimes

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

        entries = list((info or {}).get("entries") or [])
        results: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            if not entry:
                continue
            video_id = str(entry.get("id") or "")
            url = entry.get("webpage_url") or entry.get("url")
            if video_id and (
                not isinstance(url, str)
                or not url.startswith(("http://", "https://"))
            ):
                url = f"https://www.youtube.com/watch?v={video_id}"

            thumbnail = entry.get("thumbnail")
            if not thumbnail:
                thumbs = entry.get("thumbnails") or []
                if thumbs:
                    thumbnail = thumbs[-1].get("url")
            if not thumbnail and video_id:
                thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

            duration = entry.get("duration")
            duration_string = entry.get("duration_string")
            if duration_string is None and isinstance(duration, (int, float)):
                duration_string = self._format_duration(duration)

            results.append(
                {
                    "id": video_id or None,
                    "title": entry.get("title"),
                    "url": url,
                    "thumbnail": thumbnail,
                    "duration": duration,
                    "duration_string": duration_string,
                    "channel": entry.get("channel"),
                    "channel_id": entry.get("channel_id"),
                    "uploader": entry.get("uploader"),
                    "uploader_id": entry.get("uploader_id"),
                    "view_count": entry.get("view_count"),
                    "live_status": entry.get("live_status"),
                }
            )
        return results

    @staticmethod
    def _format_duration(duration: int | float) -> str:
        """Format a duration in seconds without resolving every search result."""
        seconds = max(0, int(round(duration)))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @callback
    def _set_job_status(self, job_id: str, status: str) -> None:
        """Set a job status on the Home Assistant event loop."""
        if job := self.jobs.get(job_id):
            job.status = status
            self.async_publish_state()

    @callback
    def _handle_progress(self, job_id: str, data: dict[str, Any]) -> None:
        """Apply a yt-dlp progress callback on the HA event loop."""
        job = self.jobs.get(job_id)
        if job is None:
            return

        job.title = data.get("title") or job.title
        filename = (
            data.get("filename")
            or data.get("tmpfilename")
            or data.get("info_filename")
        )
        if filename:
            job.filename = os.path.basename(str(filename))

        status = data.get("status")
        if status == "downloading":
            job.status = "downloading"
            job.downloaded_bytes = int(data.get("downloaded_bytes") or 0)
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            job.total_bytes = int(total) if total else None
            speed = data.get("speed")
            job.speed = float(speed) if speed else None
            eta = data.get("eta")
            job.eta = float(eta) if eta is not None else None
        elif status == "finished":
            job.status = "postprocessing"
        elif status == "error":
            job.status = "error"

        self.async_publish_state()

    @callback
    def _handle_postprocess_progress(self, job_id: str, data: dict[str, Any]) -> None:
        """Update state while ffmpeg/postprocessors are running."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        if data.get("status") in ("started", "processing"):
            job.status = "postprocessing"
        self.async_publish_state()

    @callback
    def async_publish_state(self) -> None:
        """Publish backward-compatible active-download progress."""
        if self._stopping:
            return

        active_jobs = [
            job
            for job in self.jobs.values()
            if job.status not in ("completed", "error", "cancelled")
        ]
        attributes: dict[str, dict[str, int | float | str]] = {}
        for job in active_jobs:
            if not job.filename:
                continue
            attributes[job.filename] = {
                "speed": job.speed or 0,
                "downloaded": job.downloaded_bytes,
                "total": job.total_bytes if job.total_bytes is not None else "Nan",
                "eta": job.eta or 0,
            }

        # Keep the original state/attribute contract: the state is the number of
        # active jobs and each attribute key is a filename. Existing Lovelace
        # cards iterate every attribute and expect speed/downloaded/total/eta.
        self.hass.states.async_set(STATE_DOWNLOADER, str(len(active_jobs)), attributes)

    @callback
    def _prune_finished_jobs(self) -> None:
        """Keep bounded history so a long-running HA instance does not leak jobs."""
        finished = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status in ("completed", "error", "cancelled")
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )
        for old_job in finished[MAX_RETAINED_JOBS:]:
            self.jobs.pop(old_job.job_id, None)

    @staticmethod
    def job_response(job: DownloadJob) -> dict[str, Any]:
        """Convert a job to a JSON-safe action/state response."""
        progress: float | None = None
        if job.total_bytes and job.total_bytes > 0:
            progress = round(min(100.0, job.downloaded_bytes * 100 / job.total_bytes), 2)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "media_type": job.request.media_type,
            "title": job.title,
            "filename": job.filename,
            "downloaded_bytes": job.downloaded_bytes,
            "total_bytes": job.total_bytes,
            "progress": progress,
            "speed": job.speed,
            "eta": job.eta,
            "final_files": list(job.final_files),
            "error": job.error,
        }

    @staticmethod
    def _extract_final_files(info: Mapping[str, Any]) -> list[str]:
        """Find final files from yt-dlp's returned info dictionary."""
        files: list[str] = []

        def add(path: Any) -> None:
            if isinstance(path, str) and path and path not in files:
                files.append(path)

        add(info.get("filepath"))
        for item in info.get("requested_downloads") or []:
            if isinstance(item, Mapping):
                add(item.get("filepath"))
        for item in info.get("entries") or []:
            if isinstance(item, Mapping):
                for path in YoutubeDlpManager._extract_final_files(item):
                    add(path)
        return files

    def _find_final_files_by_id(self, info: Mapping[str, Any]) -> list[str]:
        """Fallback final-file lookup when a postprocessor changed the filepath."""
        video_id = str(info.get("id") or "").strip()
        if not video_id:
            return []

        marker = f"[{video_id}]"
        try:
            candidates = [
                path
                for path in Path(self.download_path).iterdir()
                if path.is_file()
                and marker in path.name
                and not path.name.endswith((".part", ".ytdl"))
            ]
        except OSError:
            return []
        def modified_time(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        candidates.sort(key=modified_time, reverse=True)
        return [str(path) for path in candidates if path.exists()]

    def _cleanup_job_temp(self, job_id: str) -> None:
        """Remove all .part/fragments for a finished/failed job.

        Temp files live in a hidden per-job directory, so a failed transfer
        never leaves `song.mp4.part(s)` beside the finished media files.
        """
        path = Path(self.download_path, TEMP_DIR_NAME, job_id)
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            _LOGGER.debug("Could not clean temporary directory %s", path, exc_info=True)

    async def async_shutdown(self) -> None:
        """Cancel active workers when the config entry unloads."""
        self._stopping = True
        tasks: list[asyncio.Task[Any]] = []
        for job in self.jobs.values():
            if job.status not in ("completed", "error", "cancelled"):
                job.cancel_event.set()
                if job.task is not None:
                    tasks.append(job.task)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if pending:
                _LOGGER.warning(
                    "%d yt-dlp worker(s) exceeded the shutdown grace period; cancelling HA tasks",
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
