"""YouTube-DLP integration for Home Assistant.

Startup is intentionally minimal and deterministic. The config entry publishes
its in-memory runtime immediately; filesystem checks, FFmpeg discovery, storage
loads, frontend registration, DLNA preparation and yt-dlp/Node execution are
all deferred until Home Assistant has started or until the user invokes a media
operation.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_FILE_PATH
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.importlib import async_import_module
from homeassistant.helpers.start import async_at_started
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
from .helpers import normalize_download_directory
from .manager import YoutubeDlpManager
from .media_http import YoutubeDlpMediaView, YoutubeDlpStreamView
from .play_runtime import get_playback_manager
from .play_services import async_register_play_services
from .playback import PlaybackManager

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_get_loaded_manager = get_loaded_manager

_DATA_MEDIA_VIEW = f"{DOMAIN}_media_view_registered"
_DATA_STREAM_VIEW = f"{DOMAIN}_stream_view_registered"
_DATA_TV_VIEW = f"{DOMAIN}_tv_view_registered"
_DATA_DLNA_VIEW = f"{DOMAIN}_dlna_view_registered"
_DATA_CORE_SERVICES = f"{DOMAIN}_core_services_registered"
_DATA_TARGET_SERVICES = f"{DOMAIN}_target_services_registered"
_DATA_FAVORITES_SERVICES = f"{DOMAIN}_favorites_services_registered"


def get_favorites_store(hass: HomeAssistant):
    """Compatibility lookup imported lazily so Favorites cannot break core load."""
    from .favorites_runtime import get_favorites_store as _get

    return _get(hass)


def get_favorites_playback_controller(hass: HomeAssistant):
    """Compatibility lookup imported lazily so Favorites cannot break core load."""
    from .favorites_runtime import get_favorites_playback_controller as _get

    return _get(hass)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register only the protected core HTTP endpoints and services.

    Optional DLNA/TV/Favorites/frontend modules are deliberately not imported
    here. Home Assistant imports this module in its import executor, but this
    coroutine itself stays tiny and deterministic on the event loop.
    """
    if not hass.data.get(_DATA_MEDIA_VIEW):
        hass.http.register_view(YoutubeDlpMediaView())
        hass.data[_DATA_MEDIA_VIEW] = True
    if not hass.data.get(_DATA_STREAM_VIEW):
        hass.http.register_view(YoutubeDlpStreamView())
        hass.data[_DATA_STREAM_VIEW] = True

    if not hass.data.get(_DATA_CORE_SERVICES):
        async_register_download_services(hass)
        async_register_play_services(hass)
        hass.data[_DATA_CORE_SERVICES] = True

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Publish the core runtime without blocking Home Assistant startup."""
    # No filesystem access here. A disconnected NAS/media mount must not be able
    # to turn the config entry into Not loaded during a Home Assistant restart.
    download_path = normalize_download_directory(str(entry.data.get(CONF_FILE_PATH) or ""))
    library_path = normalize_download_directory(
        str(entry.data.get(CONF_MEDIA_LIBRARY_PATH) or download_path)
    )

    # FFmpeg is resolved lazily by the existing worker code. Passing None avoids
    # coupling config-entry setup to whether Home Assistant's optional ffmpeg
    # integration has finished initializing at this exact point in startup.
    manager = YoutubeDlpManager(hass, entry, download_path, None)
    manager.playback_manager = PlaybackManager(hass, entry, library_path)
    entry.runtime_data = manager
    manager.async_publish_state()

    # All convenience features start only after EVENT_HOMEASSISTANT_STARTED. This
    # makes them incapable of extending or failing the integration startup path.
    @callback
    def _schedule_post_start(_hass: HomeAssistant) -> None:
        entry.async_create_background_task(
            hass,
            _async_start_optional_runtime(hass, entry, manager),
            "yt_dlp_post_start",
        )

    entry.async_on_unload(async_at_started(hass, _schedule_post_start))

    _LOGGER.info(
        "YouTube-DLP core ready without startup I/O: output=%s library=%s",
        download_path,
        library_path,
    )
    return True


async def _async_start_optional_runtime(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manager: YoutubeDlpManager,
) -> None:
    """Start non-critical conveniences after Home Assistant is fully running."""
    if getattr(entry, "runtime_data", None) is not manager:
        return

    # Register optional services/views only after Home Assistant reports that
    # startup has completed. Each layer is independent and idempotent.
    if not hass.data.get(_DATA_TARGET_SERVICES):
        try:
            module = await async_import_module(
                hass, f"{__package__}.target_services"
            )
            module.async_register_target_services(hass)
            hass.data[_DATA_TARGET_SERVICES] = True
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Managed target services failed after startup; core remains available"
            )

    if not hass.data.get(_DATA_TV_VIEW):
        try:
            module = await async_import_module(hass, f"{__package__}.tv_playback")
            hass.http.register_view(module.YoutubeDlpTvStreamView())
            hass.data[_DATA_TV_VIEW] = True
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "TV relay failed after startup; core Play/Download remain available"
            )

    if not hass.data.get(_DATA_DLNA_VIEW):
        try:
            module = await async_import_module(hass, f"{__package__}.dlna")
            hass.http.register_view(module.YoutubeDlpDlnaView())
            hass.data[_DATA_DLNA_VIEW] = True
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "DLNA relay failed after startup; core Play/Download remain available"
            )

    if not hass.data.get(_DATA_FAVORITES_SERVICES):
        try:
            module = await async_import_module(
                hass, f"{__package__}.favorites_services"
            )
            module.async_register_favorites_services(hass)
            hass.data[_DATA_FAVORITES_SERVICES] = True
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Favorites services failed after startup; core remains available"
            )

    # Announcement resume listeners are event subscriptions only, but they are
    # not needed to establish core Play/Download readiness.
    playback = getattr(manager, "playback_manager", None)
    if isinstance(playback, PlaybackManager):
        try:
            playback.async_start_resume_monitoring()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Playback resume monitoring failed to start")

    # DLNA manager construction is in-memory only. FFmpeg remains lazily resolved
    # when a DLNA play request actually needs transcoding.
    try:
        module = await async_import_module(hass, f"{__package__}.dlna")
        if getattr(manager, "dlna_manager", None) is None:
            manager.dlna_manager = module.DlnaPlaybackManager(hass, None)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "DLNA runtime failed to initialize after startup; core remains available"
        )

    # Favorites storage is deliberately loaded after HA has fully started.
    try:
        favorites_module = await async_import_module(
            hass, f"{__package__}.favorites"
        )
        playback_module = await async_import_module(
            hass, f"{__package__}.favorites_playback"
        )

        if getattr(manager, "favorites_store", None) is None:
            manager.favorites_store = favorites_module.FavoritesStore(hass)
        if getattr(manager, "favorites_playback", None) is None:
            manager.favorites_playback = playback_module.FavoritesPlaybackController(
                hass, entry, manager.playback_manager, manager.favorites_store
            )
        await manager.favorites_playback.async_start()
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Favorites runtime failed after startup; core Play/Download remain available"
        )

    # Frontend/Lovelace resource I/O is also strictly post-start.
    try:
        frontend_module = await async_import_module(
            hass, f"{__package__}.frontend"
        )
        integration_version = async_get_loaded_integration(hass, DOMAIN).version
        card_version = (
            str(integration_version) if integration_version is not None else VERSION
        )
        await frontend_module.async_register_media_card(hass, card_version)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Dashboard card registration failed after startup; core remains available"
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload runtime managers without unregistering global services."""
    manager = getattr(entry, "runtime_data", None)
    if isinstance(manager, YoutubeDlpManager):
        favorites_playback = getattr(manager, "favorites_playback", None)
        stop = getattr(favorites_playback, "async_stop", None)
        if callable(stop):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Favorites cleanup failed during unload")

        playback = getattr(manager, "playback_manager", None)
        if isinstance(playback, PlaybackManager):
            playback.async_stop_resume_monitoring()

        dlna_manager = getattr(manager, "dlna_manager", None)
        if dlna_manager is not None:
            try:
                shutdown = getattr(dlna_manager, "async_shutdown", None)
                if callable(shutdown):
                    await shutdown()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("DLNA cleanup failed during unload")

        await manager.async_shutdown()

    hass.states.async_remove(STATE_DOWNLOADER)
    hass.states.async_remove(STATE_FAVORITES_PLAYBACK)
    return True
