"""YouTube-DLP integration for Home Assistant.

The download and speaker-playback boundaries are intentionally usable even if
an optional Favorites/dashboard feature fails to load.  This prevents future
UI/Favorites work from taking down the known-good download/play paths.
"""

from __future__ import annotations

import logging

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_FILE_PATH
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_loaded_integration

from .const import (
    CONF_MEDIA_LIBRARY_PATH,
    DOMAIN,
    STATE_DOWNLOADER,
    STATE_FAVORITES_PLAYBACK,
    VERSION,
)
from .download_runtime import get_loaded_manager
from .download_services import async_register_download_services
from .dlna import DlnaPlaybackManager, YoutubeDlpDlnaView
from .helpers import normalize_download_directory
from .manager import YoutubeDlpManager
from .media_http import YoutubeDlpMediaView, YoutubeDlpStreamView
from .play_runtime import get_playback_manager
from .play_services import async_register_play_services
from .playback import PlaybackManager

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_get_loaded_manager = get_loaded_manager


def get_favorites_store(hass: HomeAssistant):
    """Compatibility lookup imported lazily so Favorites cannot break core load."""
    from .favorites_runtime import get_favorites_store as _get

    return _get(hass)


def get_favorites_playback_controller(hass: HomeAssistant):
    """Compatibility lookup imported lazily so Favorites cannot break core load."""
    from .favorites_runtime import get_favorites_playback_controller as _get

    return _get(hass)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register protected core services first, optional features second."""
    hass.http.register_view(YoutubeDlpMediaView())
    hass.http.register_view(YoutubeDlpStreamView())
    hass.http.register_view(YoutubeDlpDlnaView())

    # Critical boundaries: if these fail, the integration should fail loudly.
    async_register_download_services(hass)
    async_register_play_services(hass)

    # Favorites is additive. An error here is isolated and cannot unregister or
    # wrap the already-registered download/direct-play services.
    try:
        from .favorites_services import async_register_favorites_services

        async_register_favorites_services(hass)
    except Exception:  # noqa: BLE001 - optional feature isolation is intentional
        _LOGGER.exception(
            "Favorites services failed to load; core play/download remain available"
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up core runtime without blocking I/O during Home Assistant startup."""
    # Never touch the configured media filesystem on startup. NAS/mount outages
    # are handled only when a download or library scan is requested.
    download_path = normalize_download_directory(entry.data[CONF_FILE_PATH])
    ffmpeg_path = get_ffmpeg_manager(hass).binary

    # Critical core runtime. Downloader and speaker playback are separate objects
    # and neither imports/calls the other engine.
    manager = YoutubeDlpManager(hass, entry, download_path, ffmpeg_path)
    library_path = normalize_download_directory(
        entry.data.get(CONF_MEDIA_LIBRARY_PATH, download_path)
    )
    manager.playback_manager = PlaybackManager(hass, entry, library_path)
    manager.dlna_manager = DlnaPlaybackManager(hass, ffmpeg_path)
    manager.playback_manager.async_start_resume_monitoring()
    entry.runtime_data = manager
    manager.async_publish_state()

    # Favorites is optional/additive and storage load is background-only.
    try:
        from .favorites import FavoritesStore
        from .favorites_playback import FavoritesPlaybackController

        manager.favorites_store = FavoritesStore(hass)
        manager.favorites_playback = FavoritesPlaybackController(
            hass, entry, manager.playback_manager, manager.favorites_store
        )
        entry.async_create_background_task(
            hass,
            manager.favorites_playback.async_start(),
            "yt_dlp_favorites_playback_start",
        )
    except Exception:  # noqa: BLE001 - keep protected core available
        _LOGGER.exception(
            "Favorites runtime failed to initialize; core play/download remain available"
        )

    # Frontend registration is also non-critical and never blocks entry setup.
    try:
        from .frontend import async_register_media_card

        integration_version = async_get_loaded_integration(hass, DOMAIN).version
        card_version = (
            str(integration_version) if integration_version is not None else VERSION
        )
        entry.async_create_background_task(
            hass,
            async_register_media_card(hass, card_version),
            "yt_dlp_frontend_registration",
        )
    except Exception:  # noqa: BLE001 - a card issue must not break services
        _LOGGER.exception(
            "Dashboard card registration failed; core play/download remain available"
        )

    _LOGGER.info(
        "YouTube-DLP ready: output=%s library=%s", download_path, library_path
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload runtime managers without unregistering global services."""
    manager = entry.runtime_data
    if isinstance(manager, YoutubeDlpManager):
        favorites_playback = getattr(manager, "favorites_playback", None)
        stop = getattr(favorites_playback, "async_stop", None)
        if callable(stop):
            try:
                await stop()
            except Exception:  # noqa: BLE001 - unload core even if optional cleanup fails
                _LOGGER.exception("Favorites cleanup failed during unload")

        playback = getattr(manager, "playback_manager", None)
        if isinstance(playback, PlaybackManager):
            playback.async_stop_resume_monitoring()
        dlna_manager = getattr(manager, "dlna_manager", None)
        if isinstance(dlna_manager, DlnaPlaybackManager):
            await dlna_manager.async_shutdown()
        await manager.async_shutdown()

    hass.states.async_remove(STATE_DOWNLOADER)
    hass.states.async_remove(STATE_FAVORITES_PLAYBACK)
    return True
