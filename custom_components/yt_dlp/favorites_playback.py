"""Persistent Favorites playback queue for the bundled media card.

The queue deliberately lives in Home Assistant instead of the browser. This
keeps selected tracks, repeat mode and automatic queue advancement alive when a
phone app/browser is reloaded, backgrounded or completely closed.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import logging
import time
from typing import Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SERVICE_PLAY, SERVICE_PLAY_MULTI, STATE_FAVORITES_PLAYBACK
from .favorites import FavoritesStore
from .playback import PlaybackManager
from .target_playback import async_play_url_on_targets

_LOGGER = logging.getLogger(__name__)

_STORAGE_KEY = "yt_dlp.favorites_playback"
_STORAGE_VERSION = 1
_MAX_SELECTIONS = 1000
_REPEAT_MODES = frozenset({"off", "one", "all"})
_KINDS = frozenset({"online", "offline"})
_END_MARGIN_SECONDS = 1.8
_RESTORE_DELAY_SECONDS = 2.0


def _unique_strings(values: object, *, limit: int = _MAX_SELECTIONS) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _media_position(
    state: State | None, fallback_duration: float | None
) -> tuple[float, float | None]:
    if state is None:
        return 0.0, fallback_duration
    attrs = state.attributes
    try:
        position = float(attrs.get("media_position") or 0.0)
    except (TypeError, ValueError):
        position = 0.0

    updated_at = attrs.get("media_position_updated_at")
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated_at = None
    if state.state == "playing" and isinstance(updated_at, datetime):
        try:
            now = (
                datetime.now(updated_at.tzinfo) if updated_at.tzinfo else datetime.now()
            )
            position += max(0.0, (now - updated_at).total_seconds())
        except (TypeError, ValueError):
            pass

    duration: float | None = fallback_duration
    try:
        raw_duration = attrs.get("media_duration")
        if raw_duration is not None:
            parsed = float(raw_duration)
            if parsed > 0:
                duration = parsed
    except (TypeError, ValueError):
        pass
    return max(0.0, position), duration


class FavoritesPlaybackController:
    """Persist selections and own Favorites queue playback in the HA backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        playback: PlaybackManager,
        favorites: FavoritesStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.playback = playback
        self.favorites = favorites
        self._store = Store[dict[str, Any]](
            hass,
            _STORAGE_VERSION,
            _STORAGE_KEY,
            serialize_in_event_loop=False,
        )
        self._lock = asyncio.Lock()
        self._play_lock = asyncio.Lock()
        self._loaded = False
        self._stopping = False
        self._advancing = False
        self._revision = 0
        self._generation = 0
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_service: CALLBACK_TYPE | None = None
        self._restore_task: asyncio.Task[None] | None = None
        self._advance_task: asyncio.Task[None] | None = None
        self._detach_task: asyncio.Task[None] | None = None

        self._repeat_mode = "off"
        self._online_selected: list[str] = []
        self._offline_selected: list[str] = []
        self._active = False
        self._kind: str | None = None
        self._queue: list[str] = []
        self._cursor = 0
        self._players: list[str] = []
        self._active_repeat_mode = "off"
        self._selection_bound = False
        self._current: dict[str, Any] | None = None
        self._started_at = 0.0

    async def async_start(self) -> None:
        """Load state in the background and begin monitoring the primary player."""
        if self._unsub_state is None:
            self._unsub_state = self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._handle_state_changed_event
            )
        if self._unsub_service is None:
            self._unsub_service = self.hass.bus.async_listen(
                EVENT_CALL_SERVICE, self._handle_call_service_event
            )
        await self._async_ensure_loaded()
        self._publish_state()
        if self._active and self._queue and self._players and not self._stopping:
            self._restore_task = self.entry.async_create_background_task(
                self.hass,
                self._async_restore_active_session(),
                "yt_dlp_favorites_restore",
            )

    async def async_stop(self) -> None:
        """Stop background tasks/listeners without changing persisted user state."""
        self._stopping = True
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_service is not None:
            self._unsub_service()
            self._unsub_service = None
        for task in (self._restore_task, self._advance_task, self._detach_task):
            if task is not None and not task.done():
                task.cancel()
        self._restore_task = None
        self._advance_task = None
        self._detach_task = None
        self.hass.states.async_remove(STATE_FAVORITES_PLAYBACK)

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            data = await self._store.async_load()
            if isinstance(data, dict):
                repeat_mode = str(data.get("repeat_mode") or "off")
                self._repeat_mode = repeat_mode if repeat_mode in _REPEAT_MODES else "off"
                self._online_selected = _unique_strings(data.get("online_selected"))
                self._offline_selected = _unique_strings(data.get("offline_selected"))
                self._active = bool(data.get("active"))
                kind = data.get("kind")
                self._kind = str(kind) if kind in _KINDS else None
                self._queue = _unique_strings(data.get("queue"))
                self._players = [
                    entity_id
                    for entity_id in _unique_strings(data.get("media_players"), limit=32)
                    if entity_id.startswith("media_player.")
                ]
                try:
                    self._cursor = max(0, int(data.get("cursor") or 0))
                except (TypeError, ValueError):
                    self._cursor = 0
                if self._queue:
                    self._cursor = min(self._cursor, len(self._queue) - 1)
                else:
                    self._cursor = 0
                active_repeat = str(data.get("active_repeat_mode") or self._repeat_mode)
                self._active_repeat_mode = (
                    active_repeat if active_repeat in _REPEAT_MODES else "off"
                )
                self._selection_bound = bool(data.get("selection_bound", True))
                current = data.get("current")
                self._current = dict(current) if isinstance(current, dict) else None
                try:
                    self._started_at = float(data.get("started_at") or 0.0)
                except (TypeError, ValueError):
                    self._started_at = 0.0
                try:
                    self._revision = max(0, int(data.get("revision") or 0))
                except (TypeError, ValueError):
                    self._revision = 0

                if not self._kind or not self._queue or not self._players:
                    self._active = False
                    self._kind = None
                    self._queue = []
                    self._cursor = 0
                    self._players = []
                    self._active_repeat_mode = "off"
                    self._selection_bound = False
                    self._current = None
            self._loaded = True

    def _snapshot(self) -> dict[str, Any]:
        current_key = (
            self._queue[self._cursor]
            if self._queue and self._cursor < len(self._queue)
            else None
        )
        return {
            "repeat_mode": self._repeat_mode,
            "online_selected": list(self._online_selected),
            "offline_selected": list(self._offline_selected),
            "active": self._active,
            "kind": self._kind,
            "queue": list(self._queue),
            "cursor": self._cursor,
            "current_key": current_key,
            "media_players": list(self._players),
            "active_repeat_mode": self._active_repeat_mode,
            "selection_bound": self._selection_bound,
            "current": deepcopy(self._current),
            "started_at": self._started_at,
            "revision": self._revision,
        }

    async def _async_save_locked(self) -> None:
        self._revision += 1
        await self._store.async_save(self._snapshot())
        self._publish_state()

    @callback
    def _publish_state(self) -> None:
        if not self._loaded:
            return
        snapshot = self._snapshot()
        self.hass.states.async_set(
            STATE_FAVORITES_PLAYBACK,
            "active" if self._active else "idle",
            snapshot,
        )

    async def async_get_state(self) -> dict[str, Any]:
        await self._async_ensure_loaded()
        return self._snapshot()

    async def async_update_settings(
        self,
        *,
        online_selected: list[str],
        offline_selected: list[str],
        repeat_mode: str,
    ) -> dict[str, Any]:
        """Persist synchronized Favorites selections/repeat mode."""
        await self._async_ensure_loaded()
        repeat = repeat_mode if repeat_mode in _REPEAT_MODES else "off"
        online = _unique_strings(online_selected)
        offline = _unique_strings(offline_selected)
        async with self._lock:
            self._online_selected = online
            self._offline_selected = offline
            self._repeat_mode = repeat

            # Selected-queue playback follows live checkbox edits. A directly
            # clicked single track is intentionally detached from the checkbox
            # selection and only honors the one-track repeat mode.
            if self._active and self._kind in _KINDS and self._queue:
                if self._selection_bound:
                    selected = online if self._kind == "online" else offline
                    current_key = self._queue[self._cursor]
                    future = [key for key in selected if key != current_key]
                    self._queue = [current_key, *future]
                    self._cursor = 0
                    self._active_repeat_mode = repeat
                else:
                    self._active_repeat_mode = "one" if repeat == "one" else "off"
            await self._async_save_locked()
            return self._snapshot()

    async def async_start_queue(
        self,
        *,
        kind: str,
        queue: list[str],
        media_players: list[str],
        repeat_mode: str,
        replace_selection: bool,
    ) -> dict[str, Any]:
        """Start one Favorites queue and keep advancing it in Home Assistant."""
        await self._async_ensure_loaded()
        if kind not in _KINDS:
            raise ValueError("Unsupported Favorites playback kind")
        clean_queue = _unique_strings(queue)
        if not clean_queue:
            raise ValueError("Favorites queue is empty")
        players = [
            entity_id
            for entity_id in _unique_strings(media_players, limit=32)
            if entity_id.startswith("media_player.") and self.hass.states.get(entity_id) is not None
        ]
        if not players:
            raise ValueError("No available media_player was selected")

        repeat = repeat_mode if repeat_mode in _REPEAT_MODES else "off"
        async with self._lock:
            if replace_selection:
                if kind == "online":
                    self._online_selected = list(clean_queue)
                else:
                    self._offline_selected = list(clean_queue)
                self._repeat_mode = repeat
            self._generation += 1
            generation = self._generation
            self._active = True
            self._kind = kind
            self._queue = list(clean_queue)
            self._cursor = 0
            self._players = players
            self._active_repeat_mode = repeat
            self._selection_bound = bool(replace_selection)
            self._current = None
            self._started_at = time.time()
            await self._async_save_locked()

        try:
            await self._async_play_current()
        except Exception:
            async with self._lock:
                if self._generation == generation:
                    self._clear_active_locked()
                    await self._async_save_locked()
            raise
        return await self.async_get_state()

    async def async_skip(self, direction: str) -> dict[str, Any]:
        """Move to the previous/next Favorites queue item and play it."""
        await self._async_ensure_loaded()
        async with self._lock:
            if not self._active or not self._queue:
                return self._snapshot()
            if direction == "current":
                pass
            elif direction == "previous":
                self._cursor = max(0, self._cursor - 1)
            elif direction == "next":
                if self._cursor < len(self._queue) - 1:
                    self._cursor += 1
                elif self._active_repeat_mode == "all":
                    self._cursor = 0
                else:
                    return self._snapshot()
            else:
                raise ValueError("Unsupported skip direction")
            self._generation += 1
            await self._async_save_locked()
        await self._async_play_current()
        return await self.async_get_state()

    def _clear_active_locked(self) -> None:
        """Clear only the active playback session while keeping user settings."""
        self._active = False
        self._kind = None
        self._queue = []
        self._cursor = 0
        self._players = []
        self._active_repeat_mode = "off"
        self._selection_bound = False
        self._current = None

    async def async_stop_playback(self, *, clear_selection: bool = True) -> dict[str, Any]:
        """Stop Favorites playback; the card's Stop button clears selections."""
        await self._async_ensure_loaded()
        async with self._lock:
            players = list(self._players)
            self._generation += 1
            self._clear_active_locked()
            if clear_selection:
                self._online_selected = []
                self._offline_selected = []
            await self._async_save_locked()

        if players:
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_stop",
                    target={"entity_id": players},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001 - player implementations vary
                _LOGGER.warning("Unable to stop Favorites media players: %s", err)
        return await self.async_get_state()

    async def async_cancel_active(self, *, preserve_selection: bool = True) -> None:
        """Detach the Favorites queue without stopping replacement playback."""
        await self._async_ensure_loaded()
        async with self._lock:
            if not self._active:
                return
            self._generation += 1
            self._clear_active_locked()
            if not preserve_selection:
                self._online_selected = []
                self._offline_selected = []
            await self._async_save_locked()

    async def async_remove_online_key(self, url: str) -> None:
        """Remove a deleted favorite from persisted selection/active queue."""
        await self._async_ensure_loaded()
        play_replacement = False
        replacement_generation = -1
        async with self._lock:
            changed = False
            if url in self._online_selected:
                self._online_selected = [
                    item for item in self._online_selected if item != url
                ]
                changed = True
            if self._active and self._kind == "online" and url in self._queue:
                current_key = self._queue[self._cursor] if self._queue else None
                removing_current = current_key == url
                remaining = [item for item in self._queue if item != url]
                if removing_current:
                    self._generation += 1
                    replacement_generation = self._generation
                if not remaining:
                    self._clear_active_locked()
                else:
                    self._queue = remaining
                    if current_key in remaining:
                        self._cursor = remaining.index(current_key)
                    else:
                        self._cursor = min(self._cursor, len(remaining) - 1)
                    play_replacement = removing_current
                changed = True
            if changed:
                await self._async_save_locked()

        if play_replacement:
            try:
                await self._async_play_current()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unable to continue Favorites after deleting current item: %s",
                    err,
                )
                async with self._lock:
                    if self._generation == replacement_generation:
                        self._clear_active_locked()
                        await self._async_save_locked()

    async def _async_play_current(self) -> None:
        await self._async_ensure_loaded()
        async with self._play_lock:
            async with self._lock:
                if self._stopping or not self._active or not self._queue or not self._players:
                    return
                kind = self._kind
                key = self._queue[self._cursor]
                players = list(self._players)
                generation = self._generation
                self._advancing = True

            try:
                if kind == "online":
                    await self._async_play_online(key, players, generation)
                elif kind == "offline":
                    await self._async_play_offline(key, players, generation)
                else:
                    raise ValueError("Favorites playback kind is missing")
            finally:
                self._advancing = False

    async def _async_session_is_current(
        self, generation: int, kind: str, key: str, players: list[str]
    ) -> bool:
        async with self._lock:
            return bool(
                not self._stopping
                and self._active
                and self._generation == generation
                and self._kind == kind
                and self._queue
                and self._queue[self._cursor] == key
                and self._players == players
            )

    async def _async_stop_stale_players(
        self, players: list[str], expected_content_id: str
    ) -> None:
        """Stop only players that still expose the stale request's media id.

        A new Media Player request may intentionally replace Favorites while a
        prior play_media call is still returning. Never stop that replacement.
        """
        stale_players = []
        for entity_id in players:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            content_id = state.attributes.get(ATTR_MEDIA_CONTENT_ID)
            if isinstance(content_id, str) and content_id == expected_content_id:
                stale_players.append(entity_id)
        if not stale_players:
            return
        try:
            await self.hass.services.async_call(
                "media_player",
                "media_stop",
                target={"entity_id": stale_players},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - best-effort stale-race cleanup
            pass

    async def _async_play_online(self, url: str, players: list[str], generation: int) -> None:
        items = await self.favorites.async_list()
        item = next((entry for entry in items if entry.get("url") == url), None)
        if item is None:
            raise ValueError("Selected Online favorite no longer exists")
        if not await self._async_session_is_current(generation, "online", url, players):
            return

        info, results = await async_play_url_on_targets(
            self.hass, self.playback, url, list(players)
        )
        if not any(result.success for result in results):
            errors = "; ".join(result.error or result.entity_id for result in results)
            raise RuntimeError(errors or "No Favorites playback target succeeded")
        if not await self._async_session_is_current(generation, "online", url, players):
            # Do not stop here: mixed TV/speaker playback can use different media
            # IDs, and a newer user request may already have replaced this one.
            return

        current = {
            "key": url,
            "title": info.title if info is not None else item.get("title") or "YouTube",
            "artist": info.artist if info is not None else item.get("artist"),
            "thumbnail": info.thumbnail if info is not None else item.get("thumbnail"),
            "duration": info.duration if info is not None else item.get("duration"),
            "mime_type": info.mime_type if info is not None else "video/youtube",
        }
        async with self._lock:
            if (
                self._active
                and self._generation == generation
                and self._kind == "online"
                and self._queue
                and self._queue[self._cursor] == url
            ):
                self._current = current
                self._started_at = time.time()
                await self._async_save_locked()

    async def _async_play_offline(self, item_id: str, players: list[str], generation: int) -> None:
        items = await self.playback.async_scan_library(force=False)
        item = next((entry for entry in items if entry.get("id") == item_id), None)
        if item is None:
            raise ValueError("Selected Offline favorite no longer exists")
        if not await self._async_session_is_current(generation, "offline", item_id, players):
            return

        metadata: dict[str, object] = {
            "title": item.get("title") or item.get("filename") or "Audio"
        }
        if item.get("artist"):
            metadata["artist"] = str(item["artist"])
        await self.hass.services.async_call(
            "media_player",
            SERVICE_PLAY_MEDIA,
            service_data={
                ATTR_MEDIA_CONTENT_ID: item["media_content_id"],
                ATTR_MEDIA_CONTENT_TYPE: item.get("mime_type") or "music",
                ATTR_MEDIA_EXTRA: {"metadata": metadata},
            },
            target={"entity_id": players},
            blocking=True,
        )
        if not await self._async_session_is_current(generation, "offline", item_id, players):
            await self._async_stop_stale_players(players, str(item["media_content_id"]))
            return

        current = {
            "key": item_id,
            "title": item.get("title") or item.get("filename") or "Audio",
            "artist": item.get("artist"),
            "thumbnail": None,
            "duration": item.get("duration"),
            "mime_type": item.get("mime_type") or "music",
        }
        async with self._lock:
            if (
                self._active
                and self._generation == generation
                and self._kind == "offline"
                and self._queue
                and self._queue[self._cursor] == item_id
            ):
                self._current = current
                self._started_at = time.time()
                await self._async_save_locked()

    @callback
    def _handle_call_service_event(self, event: Event) -> None:
        """Detach Favorites when the protected direct-play service is invoked.

        This listener is deliberately outside play_services.py.  Direct speaker
        playback remains unaware of Favorites, while Favorites can still hide
        its dedicated player and stop queue advancement when the user switches
        to the Media Player tab or calls yt_dlp.play/play_multi elsewhere.
        Checked selections are preserved; only Favorites Stop clears them.
        """
        if self._stopping or not self._active:
            return
        data = event.data
        if data.get("domain") != DOMAIN or data.get("service") not in {
            SERVICE_PLAY,
            SERVICE_PLAY_MULTI,
        }:
            return
        if self._detach_task is not None and not self._detach_task.done():
            return
        self._detach_task = self.entry.async_create_background_task(
            self.hass,
            self._async_detach_for_direct_play(),
            "yt_dlp_favorites_detach_direct_play",
        )

    async def _async_detach_for_direct_play(self) -> None:
        try:
            await self.async_cancel_active(preserve_selection=True)
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001 - storage errors are non-fatal to direct play
            _LOGGER.warning("Unable to detach Favorites before direct playback: %s", err)
        finally:
            self._detach_task = None

    @callback
    def _handle_state_changed_event(self, event: Event) -> None:
        if self._stopping or self._advancing or not self._active or not self._players:
            return
        data = event.data
        if data.get("entity_id") != self._players[0]:
            return
        old_state = data.get("old_state")
        new_state = data.get("new_state")
        if old_state is not None and not isinstance(old_state, State):
            return
        if new_state is not None and not isinstance(new_state, State):
            return
        if old_state is None or new_state is None:
            return
        if old_state.state not in {"playing", "paused", "buffering"}:
            return
        if new_state.state not in {"idle", "off", "standby"}:
            return

        fallback_duration: float | None = None
        try:
            raw = (self._current or {}).get("duration")
            if raw is not None:
                parsed = float(raw)
                if parsed > 0:
                    fallback_duration = parsed
        except (TypeError, ValueError):
            pass
        position, duration = _media_position(old_state, fallback_duration)

        # Only treat a transition as a natural end when the old playback was at
        # its end. Early idle transitions (manual stop, temporary Cast/TTS
        # handover) must not unexpectedly skip to the next song.
        wall_elapsed = max(0.0, time.time() - self._started_at)
        if duration is not None and duration > 0:
            near_end = position >= max(0.0, duration - _END_MARGIN_SECONDS)
            if not near_end:
                # Prefer the media player's position whenever it is exposed.
                # Falling back to wall time only helps minimal players that do
                # not report position at all; it avoids false skips on manual
                # stop after a long pause for normal HA media_player entities.
                if old_state.attributes.get("media_position") is not None:
                    return
                if wall_elapsed < max(0.0, duration - _END_MARGIN_SECONDS):
                    return
        elif wall_elapsed < 3.0:
            return
        if self._advance_task is not None and not self._advance_task.done():
            return
        self._advance_task = self.entry.async_create_background_task(
            self.hass,
            self._async_advance_after_end(),
            "yt_dlp_favorites_advance",
        )

    async def _async_advance_after_end(self) -> None:
        generation = -1
        try:
            await asyncio.sleep(0.15)
            async with self._lock:
                if self._stopping or self._advancing or not self._active or not self._queue:
                    return
                self._generation += 1
                generation = self._generation
                if self._active_repeat_mode == "one":
                    pass
                elif self._cursor < len(self._queue) - 1:
                    self._cursor += 1
                elif self._active_repeat_mode == "all":
                    self._cursor = 0
                else:
                    self._clear_active_locked()
                    await self._async_save_locked()
                    return
                await self._async_save_locked()
            await self._async_play_current()
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unable to advance Favorites playback queue: %s", err)
            async with self._lock:
                if self._generation == generation:
                    self._clear_active_locked()
                    await self._async_save_locked()
        finally:
            self._advance_task = None

    async def _async_restore_active_session(self) -> None:
        generation = -1
        try:
            await asyncio.sleep(_RESTORE_DELAY_SECONDS)
            if self._stopping:
                return
            await self._async_ensure_loaded()
            if not self._active or not self._queue or not self._players:
                return
            primary = self.hass.states.get(self._players[0])
            if primary is not None and primary.state in {"playing", "paused", "buffering"}:
                return
            async with self._lock:
                generation = self._generation
            await self._async_play_current()
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Unable to restore persisted Favorites playback: %s", err)
            # Do not leave an unrecoverable stale session shown as active. Keep
            # the user's checked songs/repeat preference intact for the next play.
            async with self._lock:
                if self._generation == generation:
                    self._clear_active_locked()
                    await self._async_save_locked()
