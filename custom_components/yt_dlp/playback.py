"""Direct playback and local media library support for YouTube-DLP.

The download worker remains in manager.py. Remote playback is relayed through
Home Assistant so speakers do not need to fetch yt-dlp's short-lived upstream
URL directly. All yt-dlp extraction and filesystem scanning run in executors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
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
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
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
_MAX_PLAYER_RELOAD_RETRIES = 3
_PLAYER_RELOAD_RETRY_DELAY_SECONDS = 0.6

# Announcement/TTS resume is handled in the integration backend, not only by
# the Lovelace card.  This keeps resume working when the dashboard is closed,
# the browser is asleep, or Cast temporarily reports incomplete media metadata.
_ANNOUNCEMENT_RESUME_IDLE_DELAY_SECONDS = 1.5
_ANNOUNCEMENT_MIN_RESUME_AGE_SECONDS = 2.5
_ANNOUNCEMENT_TRACKING_TTL_SECONDS = 10 * 60
_ANNOUNCEMENT_RESUME_ATTEMPTS = 3
_ANNOUNCEMENT_SEEK_ATTEMPTS = 6
_ANNOUNCEMENT_PLAYBACK_START_TIMEOUT_SECONDS = 12.0


# Playback intentionally starts with yt-dlp's own current default client set.
# The explicit fallbacks are only used when the default set exposes no direct
# HTTP(S) audio-capable URL that Home Assistant can actually probe.  Keeping the
# first profile as ``None`` avoids pinning playback to a client combination that
# YouTube may change independently of yt-dlp's defaults.
YOUTUBE_CLIENT_FALLBACKS: tuple[tuple[str, ...] | None, ...] = (
    None,
    ("default", "web_embedded"),
    ("android_vr",),
    ("web_embedded",),
    ("web_safari",),
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
    file_format: str
    webpage_url: str
    http_headers: dict[str, str]
    route_index: int
    format_id: str | None
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


@dataclass(slots=True)
class PlaybackResumeSession:
    """Track one yt_dlp playback so HA announcements can be restored."""

    entity_id: str
    source_url: str
    title: str
    duration: float | None
    media_source_id: str
    stream_token: str
    position: float = 0.0
    announcement_active: bool = False
    announcement_started_at: float = 0.0
    saw_foreign_media: bool = False
    saw_idle: bool = False
    restoring: bool = False
    generation: int = 0
    resume_task: asyncio.Task[None] | None = None


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
        self._resume_sessions: dict[str, PlaybackResumeSession] = {}
        self._resume_unsubs: list[CALLBACK_TYPE] = []
        self._javascript_runtime: tuple[str, str] | None | object = _JS_RUNTIME_UNSET
        self._javascript_runtime_lock = threading.Lock()

    @callback
    def async_start_resume_monitoring(self) -> None:
        """Listen for HA announcement calls and media-player state changes."""
        if self._resume_unsubs:
            return
        self._resume_unsubs = [
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._handle_call_service_event),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed_event),
        ]

    @callback
    def async_stop_resume_monitoring(self) -> None:
        """Remove announcement listeners and cancel pending resume work."""
        for unsub in self._resume_unsubs:
            unsub()
        self._resume_unsubs.clear()
        for session in self._resume_sessions.values():
            self._cancel_resume_task(session)
        self._resume_sessions.clear()

    @callback
    def async_track_remote_playback(
        self,
        entity_id: str,
        source_url: str,
        info: StreamInfo,
        media_source_id: str,
    ) -> None:
        """Remember a remote playback started by this integration."""
        previous = self._resume_sessions.pop(entity_id, None)
        if previous is not None:
            self._cancel_resume_task(previous)
        stream_token = media_source_id.rsplit("/", 1)[-1]
        session = PlaybackResumeSession(
            entity_id=entity_id,
            source_url=source_url,
            title=info.title,
            duration=float(info.duration) if isinstance(info.duration, (int, float)) else None,
            media_source_id=media_source_id,
            stream_token=stream_token,
        )
        state = self.hass.states.get(entity_id)
        if state is not None and self._state_matches_tracked_media(state, session):
            position, duration = _state_media_position(state, session.duration)
            session.position = position
            if duration is not None:
                session.duration = duration
        self._resume_sessions[entity_id] = session
        _LOGGER.debug("Tracking yt_dlp playback for announcement resume on %s", entity_id)

    @callback
    def _handle_call_service_event(self, event: Event) -> None:
        """Observe TTS/announce calls before they replace the current stream."""
        data = event.data
        domain = str(data.get("domain") or "")
        service = str(data.get("service") or "")
        service_data = data.get("service_data")
        if not isinstance(service_data, dict):
            service_data = {}

        if domain == "tts":
            targets = _merge_entity_ids(
                _service_entity_ids(service_data.get("media_player_entity_id")),
                _service_entity_ids(service_data.get("entity_id")),
                _event_target_entity_ids(data),
            )
            for entity_id in targets:
                self._mark_announcement(entity_id, f"tts.{service}")
            return

        if domain != "media_player":
            return

        targets = _merge_entity_ids(
            _service_entity_ids(service_data.get("entity_id")),
            _event_target_entity_ids(data),
        )
        if not targets:
            return

        if service == "play_media" and bool(service_data.get("announce")):
            for entity_id in targets:
                self._mark_announcement(entity_id, "media_player.play_media announce")
            return

        # A deliberate replacement/stop from the user must not be resurrected
        # later as if it were an announcement.
        if service in {"media_stop", "turn_off"} or (
            service == "play_media" and not bool(service_data.get("announce"))
        ):
            for entity_id in targets:
                session = self._resume_sessions.get(entity_id)
                if session is None or session.restoring:
                    continue
                self._cancel_resume_task(session)
                self._resume_sessions.pop(entity_id, None)

    @callback
    def _mark_announcement(self, entity_id: str, reason: str) -> None:
        session = self._resume_sessions.get(entity_id)
        if session is None or session.restoring:
            return

        # tts.speak commonly causes a nested play_media(announce=true) call.
        # Preserve the first snapshot so a second event cannot overwrite the
        # music position with the TTS clip's position.
        if session.announcement_active:
            return

        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state not in {"playing", "buffering"}
            or not self._state_matches_tracked_media(state, session)
        ):
            return
        position, duration = _state_media_position(state, session.duration)
        session.position = position
        if duration is not None:
            session.duration = duration

        if (
            session.duration is not None
            and session.duration > 0
            and session.position >= max(0.0, session.duration - 1.5)
        ):
            return

        self._cancel_resume_task(session)
        session.announcement_active = True
        session.announcement_started_at = time.monotonic()
        session.saw_foreign_media = False
        session.saw_idle = False
        session.generation += 1
        _LOGGER.info(
            "Captured yt_dlp playback before %s on %s at %.2fs",
            reason,
            entity_id,
            session.position,
        )

    @callback
    def _handle_state_changed_event(self, event: Event) -> None:
        data = event.data
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        session = self._resume_sessions.get(entity_id)
        if session is None:
            return

        new_state = data.get("new_state")
        old_state = data.get("old_state")
        if new_state is not None and not isinstance(new_state, State):
            return
        if old_state is not None and not isinstance(old_state, State):
            old_state = None

        if session.restoring:
            return

        if not session.announcement_active:
            if new_state is not None and self._state_matches_tracked_media(new_state, session):
                position, duration = _state_media_position(new_state, session.duration)
                session.position = position
                if duration is not None:
                    session.duration = duration
            if (
                old_state is not None
                and new_state is not None
                and self._state_matches_tracked_media(old_state, session)
                and new_state.state in {"idle", "off", "standby"}
            ):
                position, duration = _state_media_position(old_state, session.duration)
                if duration is not None and position >= max(0.0, duration - 1.5):
                    self._resume_sessions.pop(entity_id, None)
            return

        if time.monotonic() - session.announcement_started_at > _ANNOUNCEMENT_TRACKING_TTL_SECONDS:
            _LOGGER.warning("Announcement resume tracking expired for %s", entity_id)
            session.announcement_active = False
            self._cancel_resume_task(session)
            return

        if new_state is None:
            return

        if new_state.state in {"idle", "off", "standby"}:
            session.saw_idle = True
            self._schedule_announcement_resume(session)
            return

        if new_state.state in {"playing", "buffering", "paused"}:
            if self._state_matches_tracked_media(new_state, session):
                # Only accept automatic restoration after HA/Cast actually
                # exposed different announcement media. Some Cast updates keep
                # stale music attributes during TTS; treating those as restored
                # would clear the resume state too early.
                if session.saw_foreign_media:
                    self._cancel_resume_task(session)
                    self.hass.async_create_task(
                        self._async_seek_auto_restored(session, session.generation),
                        name=f"yt_dlp_auto_restore_{entity_id}",
                    )
                return

            session.saw_foreign_media = True
            self._cancel_resume_task(session)

    @callback
    def _schedule_announcement_resume(self, session: PlaybackResumeSession) -> None:
        self._cancel_resume_task(session)
        generation = session.generation
        elapsed = time.monotonic() - session.announcement_started_at
        delay = max(
            _ANNOUNCEMENT_RESUME_IDLE_DELAY_SECONDS,
            _ANNOUNCEMENT_MIN_RESUME_AGE_SECONDS - elapsed,
        )
        session.resume_task = self.hass.async_create_task(
            self._async_resume_after_announcement(session, generation, delay),
            name=f"yt_dlp_resume_{session.entity_id}",
        )

    @callback
    def _cancel_resume_task(self, session: PlaybackResumeSession) -> None:
        task = session.resume_task
        if task is not None and not task.done():
            task.cancel()
        session.resume_task = None

    async def _async_resume_after_announcement(
        self,
        session: PlaybackResumeSession,
        generation: int,
        delay: float,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._resume_session_is_current(session, generation):
                return
            state = self.hass.states.get(session.entity_id)
            if state is None or state.state not in {"idle", "off", "standby"}:
                return

            session.restoring = True
            last_error: Exception | None = None
            for attempt in range(1, _ANNOUNCEMENT_RESUME_ATTEMPTS + 1):
                if not self._resume_session_is_current(session, generation):
                    return
                try:
                    info, media_source_id = await self.async_create_stream(session.source_url)
                    metadata: dict[str, object] = {"title": info.title}
                    if info.artist:
                        metadata["artist"] = info.artist
                    if info.thumbnail:
                        metadata["images"] = [{"url": info.thumbnail}]

                    await self.hass.services.async_call(
                        "media_player",
                        "play_media",
                        service_data={
                            "media_content_id": media_source_id,
                            "media_content_type": info.mime_type,
                            "extra": {"metadata": metadata},
                        },
                        target={"entity_id": session.entity_id},
                        blocking=True,
                    )

                    session.title = info.title
                    session.duration = (
                        float(info.duration)
                        if isinstance(info.duration, (int, float))
                        else session.duration
                    )
                    session.media_source_id = media_source_id
                    session.stream_token = media_source_id.rsplit("/", 1)[-1]
                    await self._async_seek_resume_position(session)

                    session.announcement_active = False
                    session.saw_foreign_media = False
                    session.saw_idle = False
                    _LOGGER.info(
                        "Resumed yt_dlp playback after announcement on %s at %.2fs",
                        session.entity_id,
                        session.position,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - service/yt-dlp errors vary
                    last_error = err
                    _LOGGER.warning(
                        "Unable to resume yt_dlp playback after announcement on %s "
                        "(attempt %s/%s): %s",
                        session.entity_id,
                        attempt,
                        _ANNOUNCEMENT_RESUME_ATTEMPTS,
                        err,
                    )
                    if attempt < _ANNOUNCEMENT_RESUME_ATTEMPTS:
                        await asyncio.sleep(0.8 * attempt)

            if last_error is not None:
                _LOGGER.error(
                    "Failed to resume yt_dlp playback after announcement on %s: %s",
                    session.entity_id,
                    last_error,
                )
        except asyncio.CancelledError:
            return
        finally:
            if self._resume_sessions.get(session.entity_id) is session:
                session.restoring = False
                session.resume_task = None

    async def _async_seek_auto_restored(
        self, session: PlaybackResumeSession, generation: int
    ) -> None:
        if not self._resume_session_is_current(session, generation):
            return
        session.restoring = True
        try:
            await self._async_seek_resume_position(session)
            session.announcement_active = False
            session.saw_foreign_media = False
            session.saw_idle = False
            _LOGGER.info(
                "Re-seeked Cast auto-restored yt_dlp playback on %s to %.2fs",
                session.entity_id,
                session.position,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Cast restored yt_dlp media but seek failed on %s: %s",
                session.entity_id,
                err,
            )
        finally:
            if self._resume_sessions.get(session.entity_id) is session:
                session.restoring = False

    async def _async_seek_resume_position(self, session: PlaybackResumeSession) -> None:
        deadline = time.monotonic() + _ANNOUNCEMENT_PLAYBACK_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self.hass.states.get(session.entity_id)
            if state is not None and state.state in {"playing", "buffering"}:
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("speaker did not start restored playback")

        last_error: Exception | None = None
        for attempt in range(1, _ANNOUNCEMENT_SEEK_ATTEMPTS + 1):
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_seek",
                    service_data={"seek_position": max(0.0, session.position)},
                    target={"entity_id": session.entity_id},
                    blocking=True,
                )
                return
            except Exception as err:  # noqa: BLE001
                last_error = err
                await asyncio.sleep(0.25 * attempt)
        raise last_error or RuntimeError("unable to seek restored playback")

    @callback
    def _state_matches_tracked_media(
        self, state: State, session: PlaybackResumeSession
    ) -> bool:
        attrs = state.attributes
        content_id = str(attrs.get("media_content_id") or "")
        if session.stream_token and session.stream_token in content_id:
            return True
        if content_id == session.media_source_id:
            return True

        title = str(attrs.get("media_title") or "").strip()
        if not title or title != session.title:
            return False
        raw_duration = attrs.get("media_duration")
        if (
            session.duration is not None
            and isinstance(raw_duration, (int, float))
            and raw_duration > 0
            and abs(float(raw_duration) - session.duration) > 3.0
        ):
            return False
        return True

    @callback
    def _resume_session_is_current(
        self, session: PlaybackResumeSession, generation: int
    ) -> bool:
        return (
            self._resume_sessions.get(session.entity_id) is session
            and session.announcement_active
            and session.generation == generation
        )

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

    async def async_resolve_stream_candidates(
        self,
        url: str,
        *,
        player_clients: tuple[str, ...] | None = None,
    ) -> tuple[StreamInfo, ...]:
        """Extract real direct playback candidates without forcing a format selector.

        yt-dlp format expressions are deliberately not used here. YouTube can
        expose a different subset of formats between extractions; asking for a
        synthetic tier such as ``ba.2`` can therefore fail even though another
        perfectly usable direct URL exists. Instead, extract the actual format
        list, rank direct audio-capable HTTP(S) URLs, and let Home Assistant probe
        them in best-to-worst order.
        """
        async with self._stream_lock:
            yt_dlp_module = await async_import_module(self.hass, "yt_dlp")
            youtube_dl_cls = youtube_dl_class(yt_dlp_module)
            reload_attempt = 0
            while True:
                try:
                    return await self.hass.async_add_executor_job(
                        self._resolve_stream_candidates_sync,
                        url,
                        youtube_dl_cls,
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
                            _player_client_name(player_clients),
                            reload_attempt + 1,
                            _MAX_PLAYER_RELOAD_RETRIES,
                        )
                        await asyncio.sleep(
                            _PLAYER_RELOAD_RETRY_DELAY_SECONDS * reload_attempt
                        )
                        continue
                    raise

    def _resolve_stream_candidates_sync(
        self,
        url: str,
        youtube_dl_cls: type[Any],
        player_clients: tuple[str, ...] | None,
    ) -> tuple[StreamInfo, ...]:
        """Return actual direct formats from one yt-dlp extraction."""
        from yt_dlp.utils import DownloadError

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            # We need the extractor's real format list, not YoutubeDL's default
            # format selection. ``process=False`` below avoids selection entirely;
            # this option also prevents an extractor-level no-formats condition
            # from discarding otherwise useful metadata before we can inspect it.
            "ignore_no_formats_error": True,
            "socket_timeout": 20,
            "retries": 3,
            "extractor_retries": 3,
        }
        if js_runtimes := self._js_runtime_options():
            opts["js_runtimes"] = js_runtimes
        if player_clients:
            opts["extractor_args"] = {
                "youtube": {"player_client": list(player_clients)}
            }

        with youtube_dl_cls(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            if not isinstance(info, dict):
                raise DownloadError("yt-dlp did not return media information")

            raw_formats = info.get("formats")
            formats: list[dict[str, Any]] = []
            if isinstance(raw_formats, list):
                formats.extend(item for item in raw_formats if isinstance(item, dict))
            # Some non-YouTube extractors return one direct URL instead of a
            # ``formats`` list. Keep that supported without changing the public
            # integration behaviour.
            if not formats and isinstance(info.get("url"), str):
                formats.append(info)

            ranked = _rank_direct_playback_formats(formats)
            if not ranked:
                raise DownloadError(
                    "yt-dlp returned no direct HTTP(S) audio-capable media format"
                )

            title = str(info.get("title") or info.get("fulltitle") or "YouTube audio")
            thumbnail = _best_thumbnail(info)
            artist_value = info.get("artist") or info.get("channel") or info.get("uploader")
            artist = str(artist_value) if artist_value else None
            duration = info.get("duration")
            webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)
            base_headers = _string_headers(info.get("http_headers"))

            candidates: list[StreamInfo] = []
            seen_urls: set[str] = set()
            for fmt in ranked:
                stream_url = fmt.get("url")
                if not isinstance(stream_url, str) or stream_url in seen_urls:
                    continue
                seen_urls.add(stream_url)

                headers = dict(base_headers)
                headers.update(_string_headers(fmt.get("http_headers")))
                try:
                    cookie_header = ydl.cookiejar.get_cookie_header(stream_url)
                except (AttributeError, ValueError):
                    cookie_header = None
                if cookie_header:
                    headers["Cookie"] = str(cookie_header)

                ext = str(fmt.get("ext") or info.get("ext") or "").lower()
                acodec = str(fmt.get("acodec") or info.get("acodec") or "").lower()
                vcodec = str(fmt.get("vcodec") or info.get("vcodec") or "").lower()
                mime_type = _stream_mime_type(ext, acodec, vcodec)
                raw_format_id = fmt.get("format_id")
                format_id = str(raw_format_id) if raw_format_id is not None else None

                candidates.append(
                    StreamInfo(
                        url=stream_url,
                        title=title,
                        thumbnail=thumbnail,
                        artist=artist,
                        duration=duration,
                        mime_type=mime_type,
                        file_format=ext or _format_from_mime(mime_type),
                        webpage_url=webpage_url,
                        http_headers=headers,
                        route_index=len(candidates),
                        format_id=format_id,
                        player_clients=player_clients,
                    )
                )

            if not candidates:
                raise DownloadError("yt-dlp returned no usable direct media URL")
            return tuple(candidates)

    async def async_resolve_stream(
        self,
        url: str,
        route_indexes: tuple[int, ...] | None = None,
        *,
        player_clients: tuple[str, ...] | None = None,
    ) -> StreamInfo:
        """Resolve one candidate by best-to-worst rank for relay refreshes."""
        candidates = await self.async_resolve_stream_candidates(
            url, player_clients=player_clients
        )
        indexes = route_indexes or tuple(range(len(candidates)))
        for index in indexes:
            if 0 <= index < len(candidates):
                return candidates[index]
        raise RuntimeError("No requested direct playback candidate is available")

    async def async_create_stream(self, url: str) -> tuple[StreamInfo, str]:
        """Create a Home Assistant Media Source ID for one verified remote stream."""
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
        """Probe actual direct formats from best quality down across clients."""
        last_error: Exception | None = None

        for player_clients in YOUTUBE_CLIENT_FALLBACKS:
            try:
                candidates = await self.async_resolve_stream_candidates(
                    url, player_clients=player_clients
                )
            except Exception as err:  # noqa: BLE001 - yt-dlp public errors vary
                last_error = err
                continue

            for info in candidates:
                try:
                    status = await self._async_probe_stream(info)
                except (ClientError, TimeoutError, OSError) as err:
                    last_error = err
                    continue

                if status in _STREAM_PROBE_OK:
                    return info

                last_error = RuntimeError(
                    f"YouTube media URL rejected the probe with HTTP {status}"
                )
                if status in _STREAM_PROBE_SWITCH_CLIENT:
                    break
                # 403/404/410/416 and similar rejections can be format-specific;
                # keep stepping down through the *real* extracted candidates.

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
        """Refresh a relay URL from the latest real format list.

        ``advance_route=False`` preserves the current format id when possible;
        otherwise it chooses the same quality rank or the best available format.
        ``advance_route=True`` intentionally moves to the next lower real format.
        """
        session = self.get_stream_session(token)
        if session is None:
            return None

        previous_clients = session.info.player_clients
        selected_clients = previous_clients if player_clients is None else player_clients
        switching_client = (
            player_clients is not None and player_clients != previous_clients
        )
        candidates = await self.async_resolve_stream_candidates(
            session.source_url, player_clients=selected_clients
        )
        if not candidates:
            return None

        current_rank = session.info.route_index
        current_format_id = session.info.format_id
        matched_index = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if current_format_id
                and candidate.format_id == current_format_id
            ),
            None,
        )

        if advance_route:
            base_index = matched_index if matched_index is not None else current_rank
            selected_index = base_index + 1
            if selected_index >= len(candidates):
                return None
        else:
            if switching_client:
                # A different client exposes its own independently sorted format
                # list. Start from that client's best real candidate rather than
                # carrying over an arbitrary numeric rank from the old client.
                selected_index = 0
            elif matched_index is not None:
                selected_index = matched_index
            elif 0 <= current_rank < len(candidates):
                selected_index = current_rank
            else:
                selected_index = 0

        session.info = candidates[selected_index]
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



def _merge_entity_ids(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Merge entity-id groups while preserving service target order."""
    return tuple(dict.fromkeys(entity_id for group in groups for entity_id in group))


def _event_target_entity_ids(event_data: dict[str, Any]) -> tuple[str, ...]:
    """Read media-player entities from Home Assistant call_service target data.

    Depending on how a service was invoked, Home Assistant may expose entity ids
    in ``service_data`` or in the separate event ``target`` mapping. Supporting
    both makes announcement capture work for Developer Tools, automations,
    scripts, TTS helpers and direct service calls.
    """
    target = event_data.get("target")
    if isinstance(target, dict):
        return _service_entity_ids(target.get("entity_id"))
    return ()


def _service_entity_ids(value: object) -> tuple[str, ...]:
    """Normalize service entity-id fields without depending on target helpers."""
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set)):
        values = tuple(item for item in value if isinstance(item, str))
    else:
        return ()
    return tuple(
        dict.fromkeys(
            entity_id
            for entity_id in values
            if entity_id.startswith("media_player.")
        )
    )


def _state_media_position(
    state: State, fallback_duration: float | None = None
) -> tuple[float, float | None]:
    """Return an estimated current media position using HA's timestamp."""
    attrs = state.attributes
    try:
        position = max(0.0, float(attrs.get("media_position") or 0.0))
    except (TypeError, ValueError):
        position = 0.0

    raw_duration = attrs.get("media_duration")
    if isinstance(raw_duration, (int, float)) and raw_duration > 0:
        duration: float | None = float(raw_duration)
    else:
        duration = fallback_duration

    if state.state == "playing":
        updated_at = attrs.get("media_position_updated_at")
        if updated_at:
            try:
                stamp = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                position += max(0.0, time.time() - stamp.timestamp())
            except (TypeError, ValueError, OverflowError):
                pass

    if duration is not None and duration > 0:
        position = min(position, duration)
    return position, duration


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

def _player_client_name(player_clients: tuple[str, ...] | None) -> str:
    """Return a compact client label for logs."""
    return ",".join(player_clients) if player_clients else "yt-dlp-default"


def _string_headers(value: Any) -> dict[str, str]:
    """Normalize yt-dlp header mappings without leaking non-string values."""
    if not hasattr(value, "items"):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if item is not None
    }


def _float_value(value: Any) -> float:
    """Return a sortable non-negative numeric metadata value."""
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _rank_direct_playback_formats(
    formats: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return direct audio-capable formats in best-to-worst playback order.

    Rank by actual audio bitrate/sample-rate/channels first. Audio-only is only a
    tie-breaker, so a genuinely higher-quality muxed stream is not discarded just
    to save video bandwidth. Manifest/fragments (HLS/DASH/SABR) are excluded
    because this relay proxies one byte-addressable media resource, not an entire
    manifest and its segment graph.
    """
    candidates: list[tuple[int, bool, dict[str, Any]]] = []

    for original_index, fmt in enumerate(formats):
        stream_url = fmt.get("url")
        if not isinstance(stream_url, str) or not stream_url.startswith(("http://", "https://")):
            continue

        protocol = str(fmt.get("protocol") or "").casefold()
        if protocol and protocol not in {"http", "https"}:
            continue
        fragments = fmt.get("fragments")
        if isinstance(fragments, list) and fragments:
            continue
        if fmt.get("has_drm") is True:
            continue

        acodec = str(fmt.get("acodec") or "").casefold()
        audio_ext = str(fmt.get("audio_ext") or "").casefold()
        has_audio = (
            acodec not in {"", "none", "null", "unknown"}
            or audio_ext not in {"", "none", "null", "unknown"}
            or isinstance(fmt.get("audio_channels"), (int, float))
        )
        if not has_audio:
            continue

        vcodec = str(fmt.get("vcodec") or "").casefold()
        audio_only = vcodec in {"", "none", "null"}
        candidates.append((original_index, audio_only, fmt))

    def quality_key(
        item: tuple[int, bool, dict[str, Any]],
    ) -> tuple[float, ...]:
        original_index, audio_only, fmt = item
        abr = _float_value(fmt.get("abr"))
        # For audio-only formats yt-dlp sometimes exposes only tbr; it is a useful
        # audio bitrate fallback there. For muxed streams, tbr is dominated by
        # video and must not be mistaken for audio quality.
        effective_audio_bitrate = abr or (
            _float_value(fmt.get("tbr")) if audio_only else 0.0
        )
        return (
            effective_audio_bitrate,
            _float_value(fmt.get("asr")),
            _float_value(fmt.get("audio_channels")),
            1.0 if audio_only else 0.0,
            _float_value(fmt.get("filesize")) or _float_value(fmt.get("filesize_approx")),
            float(original_index),
        )

    candidates.sort(key=quality_key, reverse=True)
    return tuple(fmt for _, _, fmt in candidates)


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
