"""YouTube-DLP integration for Home Assistant."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_FILE_PATH
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_AUDIO_FORMAT,
    ATTR_AUDIO_QUALITY,
    ATTR_LIMIT,
    ATTR_MEDIA_TYPE,
    ATTR_OVERWRITE,
    ATTR_QUERY,
    ATTR_URL,
    ATTR_VIDEO_FORMAT,
    ATTR_VIDEO_QUALITY,
    ATTR_WAIT_FOR_COMPLETION,
    AUDIO_FORMATS,
    AUDIO_QUALITIES,
    DEFAULT_AUDIO_FORMAT,
    DEFAULT_AUDIO_QUALITY,
    DEFAULT_MEDIA_TYPE,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_VIDEO_FORMAT,
    DEFAULT_VIDEO_QUALITY,
    DOMAIN,
    MAX_SEARCH_LIMIT,
    MEDIA_TYPES,
    SERVICE_DOWNLOAD,
    SERVICE_SEARCH,
    STATE_DOWNLOADER,
    VIDEO_FORMATS,
    VIDEO_QUALITIES,
)
from .helpers import detect_external_tools, ensure_writable_directory
from .manager import DownloadRequest, YoutubeDlpManager

_LOGGER = logging.getLogger(__name__)


def _http_url(value: str) -> str:
    """Validate an HTTP(S) URL accepted by yt-dlp."""
    value = cv.string(value).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise vol.Invalid("expected an HTTP or HTTPS URL")
    return value


def _search_query(value: str) -> str:
    """Validate and normalize a search query."""
    value = cv.string(value).strip()
    if not value:
        raise vol.Invalid("search query must not be empty")
    if len(value) > 200:
        raise vol.Invalid("search query is too long")
    return value


DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): _http_url,
        vol.Optional(ATTR_MEDIA_TYPE, default=DEFAULT_MEDIA_TYPE): vol.In(MEDIA_TYPES),
        vol.Optional(ATTR_VIDEO_QUALITY, default=DEFAULT_VIDEO_QUALITY): vol.In(VIDEO_QUALITIES),
        vol.Optional(ATTR_VIDEO_FORMAT, default=DEFAULT_VIDEO_FORMAT): vol.In(VIDEO_FORMATS),
        vol.Optional(ATTR_AUDIO_FORMAT, default=DEFAULT_AUDIO_FORMAT): vol.In(AUDIO_FORMATS),
        vol.Optional(ATTR_AUDIO_QUALITY, default=DEFAULT_AUDIO_QUALITY): vol.In(AUDIO_QUALITIES),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_QUERY): _search_query,
        vol.Optional(ATTR_LIMIT, default=DEFAULT_SEARCH_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SEARCH_LIMIT)
        ),
    }
)


def _get_loaded_manager(hass: HomeAssistant) -> YoutubeDlpManager:
    """Return the single loaded manager or raise a user-facing action error."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            manager = entry.runtime_data
            if isinstance(manager, YoutubeDlpManager):
                return manager

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions independently of config entry loading."""

    async def async_download(call: ServiceCall) -> ServiceResponse | None:
        manager = _get_loaded_manager(hass)
        data = call.data

        if not manager.ffmpeg_path:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ffmpeg_missing",
            )

        request = DownloadRequest(
            url=data[ATTR_URL],
            media_type=data[ATTR_MEDIA_TYPE],
            video_quality=data[ATTR_VIDEO_QUALITY],
            video_format=data[ATTR_VIDEO_FORMAT],
            audio_format=data[ATTR_AUDIO_FORMAT],
            audio_quality=data[ATTR_AUDIO_QUALITY],
            overwrite=data[ATTR_OVERWRITE],
        )

        try:
            job = await manager.async_start_download(request)
            if data[ATTR_WAIT_FOR_COMPLETION]:
                response = await manager.async_wait_for_job(job)
                if response["status"] == "error":
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="download_failed",
                        translation_placeholders={
                            "error": response["error"] or "unknown error"
                        },
                    )
            else:
                response = manager.job_response(job)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="download_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        return response if call.return_response else None

    async def async_search(call: ServiceCall) -> ServiceResponse:
        manager = _get_loaded_manager(hass)
        query = call.data[ATTR_QUERY].strip()
        limit = call.data[ATTR_LIMIT]
        try:
            results = await manager.async_search(query, limit)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="search_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return {
            "query": query,
            "requested": limit,
            "count": len(results),
            "results": results,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_DOWNLOAD,
        async_download,
        schema=DOWNLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH,
        async_search,
        schema=SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one configured download directory."""
    try:
        download_path = await hass.async_add_executor_job(
            ensure_writable_directory, entry.data[CONF_FILE_PATH]
        )
    except (OSError, ValueError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="download_path_unavailable",
            translation_placeholders={"error": str(err)},
        ) from err

    ffmpeg_path, javascript_runtime = await hass.async_add_executor_job(
        detect_external_tools
    )
    manager = YoutubeDlpManager(
        hass, entry, download_path, ffmpeg_path, javascript_runtime
    )
    entry.runtime_data = manager
    manager.async_publish_state()

    _LOGGER.info(
        "YouTube-DLP ready: output=%s, ffmpeg=%s, javascript_runtime=%s",
        download_path,
        bool(ffmpeg_path),
        manager.javascript_runtime,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the manager without unregistering globally available actions."""
    manager = entry.runtime_data
    if isinstance(manager, YoutubeDlpManager):
        await manager.async_shutdown()

    hass.states.async_remove(STATE_DOWNLOADER)
    return True
