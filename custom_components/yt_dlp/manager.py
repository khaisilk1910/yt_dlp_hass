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
from .helpers import (
    detect_javascript_runtime,
    ensure_writable_directory,
    youtube_dl_class,
)
from .notifications import async_send_download_notifications

_LOGGER = logging.getLogger(__name__)
_JS_RUNTIME_UNSET = object()
_FFMPEG_LOCATION_UNSET = object()


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
class DownloadProgressPart:
    """Progress snapshot for one yt-dlp media stream."""

    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    finished: bool = False


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
    progress: float | None = None
    speed: float | None = None
    eta: float | None = None
    expected_parts: int = 1
    planned_total_bytes: int | None = None
    progress_parts: dict[str, DownloadProgressPart] = field(default_factory=dict, repr=False)
    final_files: list[str] = field(default_factory=list)
    error: str | None = None
    metadata_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(slots=True)
class DownloadResult:
    """Compact result returned by the blocking worker."""

    final_files: list[str]
    title: str | None
    final_size: int | None


class YoutubeDlpManager:
    """Own download/search workers for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        download_path: str,
        ffmpeg_path: str | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.download_path = download_path
        self.ffmpeg_path = ffmpeg_path
        self._ffmpeg_location: str | None | object = _FFMPEG_LOCATION_UNSET
        self._ffmpeg_location_lock = threading.Lock()
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

    def _resolve_ffmpeg_location(self) -> str | None:
        """Resolve HA's FFmpeg binary hint to an absolute executable path.

        Home Assistant normally exposes the value ``ffmpeg``. Passing that raw
        value as yt-dlp's ``ffmpeg_location`` is incorrect because yt-dlp treats
        it as a filesystem path and tests ``os.path.exists("ffmpeg")``. Resolve
        executable names through PATH first and only pass a real path to yt-dlp.
        This runs lazily in an executor worker, never during HA startup.
        """
        if self._ffmpeg_location is _FFMPEG_LOCATION_UNSET:
            with self._ffmpeg_location_lock:
                if self._ffmpeg_location is _FFMPEG_LOCATION_UNSET:
                    hint = (self.ffmpeg_path or "").strip()
                    resolved: str | None = None

                    if hint:
                        if os.path.isabs(hint) or os.path.dirname(hint):
                            if os.path.isfile(hint):
                                resolved = os.path.abspath(hint)
                            elif os.path.isdir(hint):
                                candidate = shutil.which("ffmpeg", path=hint)
                                if candidate:
                                    resolved = os.path.abspath(candidate)
                        else:
                            candidate = shutil.which(hint)
                            if candidate:
                                resolved = os.path.abspath(candidate)

                    if resolved is None:
                        candidate = shutil.which("ffmpeg")
                        if candidate:
                            resolved = os.path.abspath(candidate)

                    self._ffmpeg_location = resolved
                    if resolved:
                        _LOGGER.debug("YouTube-DLP resolved FFmpeg: %s", resolved)
                    else:
                        _LOGGER.warning(
                            "FFmpeg could not be resolved; yt-dlp will try its normal PATH lookup"
                        )

        value = self._ffmpeg_location
        return value if isinstance(value, str) else None

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

    async def async_wait_for_metadata(
        self, job: DownloadJob, timeout: float
    ) -> dict[str, Any]:
        """Wait briefly for useful response metadata while download continues."""
        if job.metadata_ready.is_set():
            return self.job_response(job)
        try:
            async with asyncio.timeout(timeout):
                await job.metadata_ready.wait()
        except TimeoutError:
            pass
        return self.job_response(job)

    def get_job_response(self, job_id: str) -> dict[str, Any] | None:
        """Return the latest snapshot for a retained job."""
        job = self.jobs.get(job_id)
        return self.job_response(job) if job is not None else None

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
                yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
                youtube_dl_cls = youtube_dl_class(yt_dlp_module)
                result = await self.hass.async_add_executor_job(
                    self._download_sync, job, youtube_dl_cls
                )
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
            job.final_files = result.final_files
            job.title = result.title or job.title
            if result.final_files:
                job.filename = os.path.basename(result.final_files[0])
            if result.final_size is not None:
                job.downloaded_bytes = result.final_size
                job.total_bytes = result.final_size
            job.progress = 100.0
            job.speed = job.speed or 0
            job.eta = 0
            job.error = None

            # Keep completion notifications fully outside the download worker and
            # outside wait_for_completion. The media job is already successful at
            # this point; unavailable notification targets can never change it to
            # an error or delay the caller waiting for the finished file.
            self.entry.async_create_background_task(
                self.hass,
                async_send_download_notifications(
                    self.hass, self.entry.options, self.job_response(job)
                ),
                f"{STATE_DOWNLOADER}_{job.job_id}_notification",
            )
        finally:
            job.metadata_ready.set()
            # Temp cleanup is owned by the executor worker itself. This avoids
            # deleting fragments underneath a worker if Home Assistant cancels
            # the async wrapper during config-entry unload.
            self._prune_finished_jobs()
            self.async_publish_state()

    def _download_sync(
        self, job: DownloadJob, youtube_dl_cls: type[Any]
    ) -> DownloadResult:
        """Run one blocking download and always clean its private temp dir."""
        # Do not validate storage during config-entry setup. A NAS/mount may be
        # temporarily offline at HA startup; validate it only when a user asks
        # for an actual download.
        ensure_writable_directory(self.download_path)
        job_temp = os.path.join(self.download_path, TEMP_DIR_NAME, job.job_id)
        try:
            return self._download_sync_worker(job, job_temp, youtube_dl_cls)
        finally:
            # This executes in the same executor thread after yt-dlp has really
            # stopped, so cleanup cannot race an in-flight downloader.
            self._cleanup_job_temp(job.job_id)

    def _download_sync_worker(
        self, job: DownloadJob, job_temp: str, youtube_dl_cls: type[Any]
    ) -> DownloadResult:
        """Blocking yt-dlp worker implementation. Executor thread only."""
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
            expected_parts, planned_total_bytes = self._progress_plan(info)
            compact = {
                "status": data.get("status"),
                "filename": data.get("filename"),
                "tmpfilename": data.get("tmpfilename"),
                "downloaded_bytes": data.get("downloaded_bytes"),
                "total_bytes": data.get("total_bytes"),
                "total_bytes_estimate": data.get("total_bytes_estimate"),
                "speed": data.get("speed"),
                "eta": data.get("eta"),
                "progress_idx": data.get("progress_idx"),
                "max_progress": data.get("max_progress"),
                "ctx_id": data.get("ctx_id"),
                "expected_parts": expected_parts,
                "planned_total_bytes": planned_total_bytes,
                "format_id": info.get("format_id") if isinstance(info, Mapping) else None,
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

        # Let the installed yt-dlp choose its current upstream YouTube clients.
        # Do not pin player clients or PO-token providers here; those assumptions
        # become stale quickly and yt-dlp 2026.08.19 already carries current
        # YouTube client maintenance/fallbacks.
        os.makedirs(job_temp, exist_ok=True)
        self.hass.add_job(self._set_job_status, job.job_id, "extracting")

        audio_selectors: tuple[str | None, ...]
        if request.media_type == MEDIA_TYPE_AUDIO:
            audio_selectors = self._audio_source_selectors()
        else:
            audio_selectors = (None,)

        extracted: Any = None
        last_error: Exception | None = None

        # First run is intentionally identical to the known-good 0.4.1 worker:
        # let yt-dlp 2026.08.19 select its default YouTube clients. Only if the
        # complete attempt set ends in an HTTP 403 do one clean retry explicitly
        # excluding android_vr. This is a narrow fallback for stale/contaminated
        # long-running interpreter state and for upstream client regressions; it
        # never changes the successful path.
        for client_fallback in (False, True):
            last_error = None
            for attempt, audio_selector in enumerate(audio_selectors, start=1):
                if job.cancel_event.is_set():
                    raise DownloadCancelled(
                        "Download cancelled because the integration is unloading"
                    )

                # Preserve the known-good worker's temp layout on the normal
                # path. The extra directory name exists only for the 403 client
                # fallback, so a successful first pass is behaviorally identical.
                if client_fallback:
                    attempt_temp = os.path.join(job_temp, f"safe-attempt-{attempt}")
                elif len(audio_selectors) > 1:
                    attempt_temp = os.path.join(job_temp, f"attempt-{attempt}")
                else:
                    attempt_temp = job_temp
                os.makedirs(attempt_temp, exist_ok=True)
                ydl_opts = self._build_download_options(
                    request,
                    attempt_temp,
                    progress_hook,
                    postprocessor_hook,
                    audio_source_selector=audio_selector,
                    avoid_android_vr=client_fallback,
                )

                try:
                    with youtube_dl_cls(ydl_opts) as ydl:
                        extracted = ydl.extract_info(request.url, download=True)
                    last_error = None
                    break
                except DownloadCancelled:
                    raise
                except DownloadError as err:
                    last_error = err
                    if self._should_retry_audio_download(
                        request, err, attempt, len(audio_selectors)
                    ):
                        next_selector = audio_selectors[attempt]
                        _LOGGER.warning(
                            "Audio source attempt %s/%s failed for job %s (%s); "
                            "retrying with fallback selector %s",
                            attempt,
                            len(audio_selectors),
                            job.job_id,
                            self._short_error(err),
                            next_selector,
                        )
                        shutil.rmtree(attempt_temp, ignore_errors=True)
                        self.hass.add_job(
                            self._reset_job_progress_for_retry, job.job_id
                        )
                        continue
                    break

            if extracted is not None:
                break

            if (
                not client_fallback
                and last_error is not None
                and self._is_http_403(last_error)
            ):
                _LOGGER.warning(
                    "YouTube returned HTTP 403 for job %s; retrying once with "
                    "android_vr excluded while preserving the same requested format",
                    job.job_id,
                )
                shutil.rmtree(job_temp, ignore_errors=True)
                os.makedirs(job_temp, exist_ok=True)
                self.hass.add_job(self._reset_job_progress_for_retry, job.job_id)
                continue

            if last_error is not None:
                raise last_error

        if extracted is None and last_error is not None:
            raise last_error

        info = extracted if isinstance(extracted, Mapping) else None

        if not info:
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
        final_size: int | None = None
        try:
            final_size = os.path.getsize(final_files[0])
        except OSError:
            pass
        return DownloadResult(
            final_files=final_files,
            title=str(info.get("title")) if info.get("title") else None,
            final_size=final_size,
        )

    def _build_download_options(
        self,
        request: DownloadRequest,
        temp_path: str,
        progress_hook: Any,
        postprocessor_hook: Any,
        *,
        audio_source_selector: str | None = None,
        avoid_android_vr: bool = False,
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

        # Never pass Home Assistant's common raw value "ffmpeg" as
        # ffmpeg_location. yt-dlp interprets ffmpeg_location as a path and will
        # disable FFmpeg when that relative path does not exist.
        if ffmpeg_location := self._resolve_ffmpeg_location():
            opts["ffmpeg_location"] = ffmpeg_location

        if js_runtimes := self._js_runtime_options():
            opts["js_runtimes"] = js_runtimes

        if avoid_android_vr:
            # API equivalent of:
            # --extractor-args "youtube:player_client=default,-android_vr"
            # Keep this strictly as a retry path; yt-dlp's defaults remain the
            # first choice and can evolve independently in future releases.
            opts["extractor_args"] = {
                "youtube": {"player_client": ["default", "-android_vr"]}
            }

        if request.media_type == MEDIA_TYPE_AUDIO:
            opts.update(self._audio_options(request, audio_source_selector))
        else:
            opts.update(self._video_options(request))
        return opts

    @staticmethod
    def _audio_source_selectors() -> tuple[str, ...]:
        """Return distinct source routes for resilient YouTube audio downloads.

        YouTube can expose a DASH format successfully and still return HTTP 403
        for its media URL. A slash-separated yt-dlp selector only falls back when
        a format is unavailable, not when the selected media URL later returns
        403. Keep the routes distinct so the worker can retry a different stream.
        """
        return (
            # M4A/MP4A is also the audio route used by our successful MP4 video
            # downloads and is generally the least expensive audio-only fallback.
            "ba[ext=m4a]/ba[acodec^=mp4a]",
            # Try the independent WebM/Opus DASH route next.
            "ba[ext=webm]/ba[acodec^=opus]",
            # Last resort: use HLS or a progressive muxed stream and let FFmpeg
            # extract the audio. This downloads video bytes too, but avoids a
            # hard failure when YouTube rejects all separate DASH audio URLs.
            "ba[protocol^=m3u8]/b[protocol^=m3u8]/b[ext=mp4]/b",
        )

    @staticmethod
    def _audio_options(
        request: DownloadRequest, source_selector: str | None = None
    ) -> dict[str, Any]:
        """Return yt-dlp options for an audio-only final file."""
        quality = "0" if request.audio_quality == "best" else request.audio_quality
        return {
            "format": source_selector or "ba[ext=m4a]/ba/b",
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
    def _short_error(error: Exception) -> str:
        """Return one compact log line for a yt-dlp failure."""
        text = " ".join(str(error).split())
        return text[:240]

    @staticmethod
    def _is_http_403(error: Exception) -> bool:
        """Return whether yt-dlp reported an upstream HTTP 403 rejection."""
        message = str(error).casefold()
        return "http error 403" in message or "403: forbidden" in message

    @staticmethod
    def _should_retry_audio_download(
        request: DownloadRequest,
        error: Exception,
        attempt: int,
        total_attempts: int,
    ) -> bool:
        """Return whether an audio failure should try another source route."""
        if request.media_type != MEDIA_TYPE_AUDIO or attempt >= total_attempts:
            return False

        message = str(error).casefold()
        return (
            "http error 403" in message
            or "403: forbidden" in message
            or "requested format is not available" in message
        )

    @staticmethod
    def _progress_plan(info: Any) -> tuple[int | None, int | None]:
        """Return selected stream count and total source bytes when yt-dlp knows them."""
        if not isinstance(info, Mapping):
            return None, None

        requested = info.get("requested_formats")
        if not isinstance(requested, (list, tuple)) or not requested:
            format_id = str(info.get("format_id") or "")
            part_ids = [item for item in format_id.split("+") if item]
            return (len(part_ids), None) if len(part_ids) > 1 else (None, None)

        total = 0
        all_sizes_known = True
        count = 0
        for item in requested:
            if not isinstance(item, Mapping):
                continue
            count += 1
            raw_size = item.get("filesize") or item.get("filesize_approx")
            try:
                size = int(raw_size) if raw_size is not None else 0
            except (TypeError, ValueError):
                size = 0
            if size > 0:
                total += size
            else:
                all_sizes_known = False

        if count < 1:
            return None, None
        return count, total if all_sizes_known and total > 0 else None

    @staticmethod
    def _progress_part_key(data: Mapping[str, Any]) -> str:
        """Build a stable key for separate video/audio or parallel media streams."""
        filename = data.get("filename") or data.get("tmpfilename")
        progress_idx = data.get("progress_idx")
        max_progress = data.get("max_progress")
        if progress_idx is not None and max_progress:
            return f"parallel:{progress_idx}:{filename or ''}"

        if filename:
            return f"file:{filename}"

        ctx_id = data.get("ctx_id")
        if ctx_id is not None:
            return f"ctx:{ctx_id}"

        format_id = data.get("format_id")
        if format_id:
            return f"format:{format_id}"
        return "stream:0"

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        """Return a positive integer or None for unreliable progress metadata."""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _update_job_transfer_progress(
        self, job: DownloadJob, data: Mapping[str, Any], *, finished: bool
    ) -> None:
        """Aggregate yt-dlp stream callbacks into one monotonic transfer progress."""
        expected_parts = self._positive_int(data.get("expected_parts"))
        max_progress = self._positive_int(data.get("max_progress"))
        if expected_parts:
            job.expected_parts = max(job.expected_parts, expected_parts)
        if max_progress:
            job.expected_parts = max(job.expected_parts, max_progress)

        planned_total = self._positive_int(data.get("planned_total_bytes"))
        if planned_total:
            job.planned_total_bytes = planned_total

        key = self._progress_part_key(data)
        part = job.progress_parts.get(key)
        if part is None:
            part = DownloadProgressPart()
            job.progress_parts[key] = part

        downloaded = max(0, int(data.get("downloaded_bytes") or 0))
        total = self._positive_int(
            data.get("total_bytes") or data.get("total_bytes_estimate")
        )
        part.downloaded_bytes = max(part.downloaded_bytes, downloaded)
        if total is not None:
            part.total_bytes = max(part.total_bytes or 0, total)
        if finished:
            part.finished = True
            if part.total_bytes is None and part.downloaded_bytes > 0:
                part.total_bytes = part.downloaded_bytes
            elif part.total_bytes is not None:
                part.downloaded_bytes = max(part.downloaded_bytes, part.total_bytes)

        raw_speed = data.get("speed")
        try:
            part.speed = float(raw_speed) if raw_speed else None
        except (TypeError, ValueError):
            part.speed = None

        aggregate_downloaded = sum(
            max(0, item.downloaded_bytes) for item in job.progress_parts.values()
        )
        known_totals = [
            item.total_bytes
            for item in job.progress_parts.values()
            if item.total_bytes is not None and item.total_bytes > 0
        ]
        all_expected_parts_seen = len(job.progress_parts) >= max(1, job.expected_parts)

        aggregate_total: int | None = None
        if (
            job.planned_total_bytes is not None
            and job.planned_total_bytes >= aggregate_downloaded
        ):
            aggregate_total = job.planned_total_bytes
        elif all_expected_parts_seen and len(known_totals) == len(job.progress_parts):
            aggregate_total = sum(known_totals)

        if aggregate_total and aggregate_total > 0:
            transfer_progress = aggregate_downloaded * 100.0 / aggregate_total
        else:
            # When yt-dlp does not expose every stream's byte size in advance,
            # combine each stream's own progress. Unseen streams contribute 0%,
            # so finishing video cannot falsely show 100% while audio is pending.
            stage_sum = 0.0
            for item in job.progress_parts.values():
                if item.finished:
                    stage_sum += 1.0
                elif item.total_bytes and item.total_bytes > 0:
                    stage_sum += min(0.999, item.downloaded_bytes / item.total_bytes)
            transfer_progress = 100.0 * stage_sum / max(
                job.expected_parts, len(job.progress_parts), 1
            )

        job.downloaded_bytes = aggregate_downloaded
        job.total_bytes = aggregate_total
        # 100% is reserved for the actual completed job. yt-dlp's "finished"
        # callback means a source stream finished and post-processing may remain.
        job.progress = round(max(0.0, min(99.0, transfer_progress)), 2)

        active_speeds = [
            item.speed for item in job.progress_parts.values() if item.speed and not item.finished
        ]
        job.speed = sum(active_speeds) if active_speeds else None
        if aggregate_total and job.speed and job.speed > 0:
            job.eta = max(0.0, (aggregate_total - aggregate_downloaded) / job.speed)
        else:
            raw_eta = data.get("eta")
            try:
                job.eta = float(raw_eta) if raw_eta is not None and not finished else None
            except (TypeError, ValueError):
                job.eta = None

    def _reset_job_progress_for_retry(self, job_id: str) -> None:
        """Clear stale byte counters before a different audio source retry."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = "extracting"
        job.filename = None
        job.downloaded_bytes = 0
        job.total_bytes = None
        job.progress = None
        job.speed = None
        job.eta = None
        job.expected_parts = 1
        job.planned_total_bytes = None
        job.progress_parts.clear()
        self.async_publish_state()

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
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            return await self.hass.async_add_executor_job(
                self._search_sync, query, limit, youtube_dl_cls
            )

    def _search_sync(
        self, query: str, limit: int, youtube_dl_cls: type[Any]
    ) -> list[dict[str, Any]]:
        """Blocking YouTube search. Executor thread only."""
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

        with youtube_dl_cls(opts) as ydl:
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
            self._update_job_transfer_progress(job, data, finished=False)
            job.metadata_ready.set()
        elif status == "finished":
            self._update_job_transfer_progress(job, data, finished=True)
            all_streams_finished = (
                len(job.progress_parts) >= max(1, job.expected_parts)
                and all(part.finished for part in job.progress_parts.values())
            )
            job.status = "postprocessing" if all_streams_finished else "downloading"
            if all_streams_finished:
                job.speed = None
                job.eta = None
            job.metadata_ready.set()
        elif status == "error":
            job.status = "error"
            job.metadata_ready.set()

        self.async_publish_state()

    @callback
    def _handle_postprocess_progress(self, job_id: str, data: dict[str, Any]) -> None:
        """Update state while ffmpeg/postprocessors are running."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        if data.get("status") in ("started", "processing"):
            job.status = "postprocessing"
            job.progress = 99.0
            job.speed = None
            job.eta = None
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
        progress = job.progress
        if job.status == "completed":
            progress = 100.0
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
            # Completion-notification details derived only from information the
            # stable 0.2.6 download job already owns. No extra yt-dlp extraction,
            # format probing, HTTP request, or filesystem scan is introduced.
            "file_size_bytes": (
                job.total_bytes if job.status == "completed" else None
            ),
            "format": YoutubeDlpManager._job_output_format(job),
            "quality": YoutubeDlpManager._job_quality(job),
            "source_url": job.request.url,
            "video_quality": job.request.video_quality,
            "video_format": job.request.video_format,
            "audio_format": job.request.audio_format,
            "audio_quality": job.request.audio_quality,
            "error": job.error,
        }

    @staticmethod
    def _job_output_format(job: DownloadJob) -> str:
        """Return the final extension without inspecting media streams."""
        if job.filename:
            suffix = Path(job.filename).suffix.lstrip(".").lower()
            if suffix:
                return suffix
        return (
            job.request.audio_format
            if job.request.media_type == MEDIA_TYPE_AUDIO
            else job.request.video_format
        )

    @staticmethod
    def _job_quality(job: DownloadJob) -> str:
        """Return the requested output quality for completion messages."""
        if job.request.media_type == MEDIA_TYPE_AUDIO:
            return (
                "best"
                if job.request.audio_quality == "best"
                else f"{job.request.audio_quality} kbps"
            )
        return (
            "best"
            if job.request.video_quality == "best"
            else f"{job.request.video_quality}p"
        )

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
