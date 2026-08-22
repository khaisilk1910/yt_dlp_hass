"""YouTube-DLP integration for Home Assistant."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_FILE_PATH
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_loaded_integration

from .const import (
    ATTR_AUDIO_FORMAT,
    ATTR_AUDIO_QUALITY,
    ATTR_FORCE,
    ATTR_JOB_ID,
    ATTR_LIMIT,
    ATTR_MEDIA_PLAYER,
    ATTR_MEDIA_PLAYERS,
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
    CONF_MEDIA_LIBRARY_PATH,
    DOMAIN,
    MAX_SEARCH_LIMIT,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPES,
    RESPONSE_METADATA_TIMEOUT,
    SERVICE_DOWNLOAD,
    SERVICE_DOWNLOAD_AUDIO,
    SERVICE_DOWNLOAD_VIDEO,
    SERVICE_GET_JOB,
    SERVICE_PLAY,
    SERVICE_PLAY_MULTI,
    SERVICE_SCAN_LIBRARY,
    SERVICE_SEARCH,
    STATE_DOWNLOADER,
    VIDEO_FORMATS,
    VIDEO_QUALITIES,
    VERSION,
)
from .helpers import normalize_download_directory
from .frontend import async_register_media_card
from .manager import DownloadRequest, YoutubeDlpManager
from .media_http import YoutubeDlpMediaView, YoutubeDlpStreamView
from .playback import PlaybackManager

_LOGGER = logging.getLogger(__name__)

# This integration is config-entry only. Defining CONFIG_SCHEMA is required for
# integrations implementing async_setup and also gives a clear HA error if a
# user tries to configure the integration from configuration.yaml.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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
        vol.Optional(ATTR_VIDEO_QUALITY, default=DEFAULT_VIDEO_QUALITY): vol.In(
            VIDEO_QUALITIES
        ),
        vol.Optional(ATTR_VIDEO_FORMAT, default=DEFAULT_VIDEO_FORMAT): vol.In(
            VIDEO_FORMATS
        ),
        vol.Optional(ATTR_AUDIO_FORMAT, default=DEFAULT_AUDIO_FORMAT): vol.In(
            AUDIO_FORMATS
        ),
        vol.Optional(ATTR_AUDIO_QUALITY, default=DEFAULT_AUDIO_QUALITY): vol.In(
            AUDIO_QUALITIES
        ),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

VIDEO_DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): _http_url,
        vol.Optional(ATTR_VIDEO_QUALITY, default=DEFAULT_VIDEO_QUALITY): vol.In(
            VIDEO_QUALITIES
        ),
        vol.Optional(ATTR_VIDEO_FORMAT, default=DEFAULT_VIDEO_FORMAT): vol.In(
            VIDEO_FORMATS
        ),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

AUDIO_DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): _http_url,
        vol.Optional(ATTR_AUDIO_FORMAT, default=DEFAULT_AUDIO_FORMAT): vol.In(
            AUDIO_FORMATS
        ),
        vol.Optional(ATTR_AUDIO_QUALITY, default=DEFAULT_AUDIO_QUALITY): vol.In(
            AUDIO_QUALITIES
        ),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

GET_JOB_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): vol.Match(r"^[0-9a-f]{32}$"),
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


def _media_player_entity(value: str) -> str:
    """Validate a single media_player entity id."""
    value = cv.entity_id(value)
    if not value.startswith("media_player."):
        raise vol.Invalid("expected a media_player entity")
    return value


PLAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): _http_url,
        vol.Required(ATTR_MEDIA_PLAYER): _media_player_entity,
    }
)


def _media_player_entities(value: object) -> list[str]:
    """Validate one or more unique media_player entity ids."""
    raw_items = [value] if isinstance(value, str) else value
    if not isinstance(raw_items, (list, tuple)):
        raise vol.Invalid("expected a list of media_player entities")

    entities: list[str] = []
    for item in raw_items:
        entity_id = _media_player_entity(item)
        if entity_id not in entities:
            entities.append(entity_id)

    if not entities:
        raise vol.Invalid("at least one media_player entity is required")
    if len(entities) > 32:
        raise vol.Invalid("a maximum of 32 media_player entities is supported")
    return entities


PLAY_MULTI_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): _http_url,
        vol.Required(ATTR_MEDIA_PLAYERS): _media_player_entities,
    }
)

SCAN_LIBRARY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
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


def get_playback_manager(hass: HomeAssistant) -> PlaybackManager:
    """Return the playback manager attached to the loaded download manager."""
    manager = _get_loaded_manager(hass)
    playback = getattr(manager, "playback_manager", None)
    if isinstance(playback, PlaybackManager):
        return playback
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions independently of config entry loading."""
    hass.http.register_view(YoutubeDlpMediaView())
    hass.http.register_view(YoutubeDlpStreamView())

    async def _run_download(
        call: ServiceCall, request: DownloadRequest
    ) -> ServiceResponse | None:
        manager = _get_loaded_manager(hass)
        try:
            job = await manager.async_start_download(request)
            if call.data[ATTR_WAIT_FOR_COMPLETION]:
                response = await manager.async_wait_for_job(job)
            elif call.return_response:
                # A returned service response is a snapshot, not a live object.
                # Wait only until yt-dlp emits its first useful progress metadata
                # so Developer Tools/response_variable usually gets title, file
                # name and progress instead of an all-null queued snapshot.
                response = await manager.async_wait_for_metadata(
                    job, RESPONSE_METADATA_TIMEOUT
                )
            else:
                response = manager.job_response(job)

            if response["status"] == "error":
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="download_failed",
                    translation_placeholders={
                        "error": response["error"] or "unknown error"
                    },
                )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="download_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        return response if call.return_response else None

    async def async_download(call: ServiceCall) -> ServiceResponse | None:
        """Backward-compatible combined video/audio action."""
        data = call.data
        request = DownloadRequest(
            url=data[ATTR_URL],
            media_type=data[ATTR_MEDIA_TYPE],
            video_quality=data[ATTR_VIDEO_QUALITY],
            video_format=data[ATTR_VIDEO_FORMAT],
            audio_format=data[ATTR_AUDIO_FORMAT],
            audio_quality=data[ATTR_AUDIO_QUALITY],
            overwrite=data[ATTR_OVERWRITE],
        )
        return await _run_download(call, request)

    async def async_download_video(call: ServiceCall) -> ServiceResponse | None:
        """Download video with only video-specific fields exposed in the UI."""
        data = call.data
        request = DownloadRequest(
            url=data[ATTR_URL],
            media_type=MEDIA_TYPE_VIDEO,
            video_quality=data[ATTR_VIDEO_QUALITY],
            video_format=data[ATTR_VIDEO_FORMAT],
            audio_format=DEFAULT_AUDIO_FORMAT,
            audio_quality=DEFAULT_AUDIO_QUALITY,
            overwrite=data[ATTR_OVERWRITE],
        )
        return await _run_download(call, request)

    async def async_download_audio(call: ServiceCall) -> ServiceResponse | None:
        """Download audio with only audio-specific fields exposed in the UI."""
        data = call.data
        request = DownloadRequest(
            url=data[ATTR_URL],
            media_type=MEDIA_TYPE_AUDIO,
            video_quality=DEFAULT_VIDEO_QUALITY,
            video_format=DEFAULT_VIDEO_FORMAT,
            audio_format=data[ATTR_AUDIO_FORMAT],
            audio_quality=data[ATTR_AUDIO_QUALITY],
            overwrite=data[ATTR_OVERWRITE],
        )
        return await _run_download(call, request)

    async def async_get_job(call: ServiceCall) -> ServiceResponse:
        """Return the latest snapshot for a background download job."""
        manager = _get_loaded_manager(hass)
        response = manager.get_job_response(call.data[ATTR_JOB_ID])
        if response is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="job_not_found",
                translation_placeholders={"job_id": call.data[ATTR_JOB_ID]},
            )
        return response

    async def async_search(call: ServiceCall) -> ServiceResponse:
        manager = _get_loaded_manager(hass)
        query = call.data[ATTR_QUERY]
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

    async def async_play(call: ServiceCall) -> ServiceResponse | None:
        """Resolve a remote audio stream and start playback on one HA player."""
        playback = get_playback_manager(hass)
        entity_id = call.data[ATTR_MEDIA_PLAYER]
        try:
            info, media_source_id = await playback.async_create_stream(
                call.data[ATTR_URL]
            )
            metadata: dict[str, object] = {"title": info.title}
            if info.artist:
                metadata["artist"] = info.artist
            if info.thumbnail:
                metadata["images"] = [{"url": info.thumbnail}]

            await hass.services.async_call(
                "media_player",
                SERVICE_PLAY_MEDIA,
                service_data={
                    ATTR_MEDIA_CONTENT_ID: media_source_id,
                    ATTR_MEDIA_CONTENT_TYPE: info.mime_type,
                    ATTR_MEDIA_EXTRA: {"metadata": metadata},
                },
                target={"entity_id": entity_id},
                blocking=True,
                context=call.context,
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        response = {"media_player": entity_id, **info.as_dict()}
        return response if call.return_response else None

    async def async_play_multi(call: ServiceCall) -> ServiceResponse | None:
        """Resolve one remote stream and start it on multiple HA players."""
        playback = get_playback_manager(hass)
        entity_ids = call.data[ATTR_MEDIA_PLAYERS]
        try:
            info, media_source_id = await playback.async_create_stream(
                call.data[ATTR_URL]
            )
            metadata: dict[str, object] = {"title": info.title}
            if info.artist:
                metadata["artist"] = info.artist
            if info.thumbnail:
                metadata["images"] = [{"url": info.thumbnail}]

            await hass.services.async_call(
                "media_player",
                SERVICE_PLAY_MEDIA,
                service_data={
                    ATTR_MEDIA_CONTENT_ID: media_source_id,
                    ATTR_MEDIA_CONTENT_TYPE: info.mime_type,
                    ATTR_MEDIA_EXTRA: {"metadata": metadata},
                },
                target={"entity_id": entity_ids},
                blocking=True,
                context=call.context,
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        response = {
            "media_players": entity_ids,
            "player_count": len(entity_ids),
            **info.as_dict(),
        }
        return response if call.return_response else None

    async def async_scan_library(call: ServiceCall) -> ServiceResponse:
        """Scan or return the cached configured local music library."""
        playback = get_playback_manager(hass)
        try:
            items = await playback.async_scan_library(force=call.data[ATTR_FORCE])
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="library_scan_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return {
            "path": playback.library_path,
            "count": len(items),
            "items": items,
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
        SERVICE_DOWNLOAD_VIDEO,
        async_download_video,
        schema=VIDEO_DOWNLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DOWNLOAD_AUDIO,
        async_download_audio,
        schema=AUDIO_DOWNLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_JOB,
        async_get_job,
        schema=GET_JOB_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH,
        async_search,
        schema=SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY,
        async_play,
        schema=PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_MULTI,
        async_play_multi,
        schema=PLAY_MULTI_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_LIBRARY,
        async_scan_library,
        schema=SCAN_LIBRARY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry without blocking I/O during Home Assistant startup."""
    # The config flow already validates/writes the path. Do not touch the file
    # system here: an offline NAS/mount must not stall or fail HA startup.
    download_path = normalize_download_directory(entry.data[CONF_FILE_PATH])

    # Reuse Home Assistant's built-in ffmpeg integration/configuration instead
    # of probing PATH ourselves. The manifest declares ffmpeg as a dependency.
    ffmpeg_path = get_ffmpeg_manager(hass).binary

    manager = YoutubeDlpManager(hass, entry, download_path, ffmpeg_path)
    library_path = normalize_download_directory(
        entry.data.get(CONF_MEDIA_LIBRARY_PATH, download_path)
    )
    manager.playback_manager = PlaybackManager(hass, entry, library_path)
    entry.runtime_data = manager
    manager.async_publish_state()

    # Resource storage/static registration is asynchronous and independent of
    # downloads. Derive the cache-busting version from manifest.json so every
    # future HACS update automatically gets a new resource URL when its version
    # changes, without maintaining a second frontend version manually.
    integration_version = async_get_loaded_integration(hass, DOMAIN).version
    card_version = str(integration_version) if integration_version is not None else VERSION
    entry.async_create_background_task(
        hass,
        async_register_media_card(hass, card_version),
        "yt_dlp_frontend_registration",
    )

    _LOGGER.info(
        "YouTube-DLP ready: output=%s library=%s", download_path, library_path
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the manager without unregistering globally available actions."""
    manager = entry.runtime_data
    if isinstance(manager, YoutubeDlpManager):
        await manager.async_shutdown()

    hass.states.async_remove(STATE_DOWNLOADER)
    return True
