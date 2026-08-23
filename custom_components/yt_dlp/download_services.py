"""Protected yt-dlp download/search service boundary.

Do not add Favorites, dashboard, speaker or media-player behavior here.  The
worker implementation stays in manager.py; this module only validates requests
and exposes the stable Home Assistant services.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_AUDIO_FORMAT,
    ATTR_AUDIO_QUALITY,
    ATTR_JOB_ID,
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
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPES,
    RESPONSE_METADATA_TIMEOUT,
    SERVICE_DOWNLOAD,
    SERVICE_DOWNLOAD_AUDIO,
    SERVICE_DOWNLOAD_VIDEO,
    SERVICE_GET_JOB,
    SERVICE_SEARCH,
    VIDEO_FORMATS,
    VIDEO_QUALITIES,
)
from .manager import DownloadRequest
from .download_runtime import get_loaded_manager
from .service_validation import http_url, search_query

DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Optional(ATTR_MEDIA_TYPE, default=DEFAULT_MEDIA_TYPE): vol.In(MEDIA_TYPES),
        vol.Optional(ATTR_VIDEO_QUALITY, default=DEFAULT_VIDEO_QUALITY): vol.In(VIDEO_QUALITIES),
        vol.Optional(ATTR_VIDEO_FORMAT, default=DEFAULT_VIDEO_FORMAT): vol.In(VIDEO_FORMATS),
        vol.Optional(ATTR_AUDIO_FORMAT, default=DEFAULT_AUDIO_FORMAT): vol.In(AUDIO_FORMATS),
        vol.Optional(ATTR_AUDIO_QUALITY, default=DEFAULT_AUDIO_QUALITY): vol.In(AUDIO_QUALITIES),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

VIDEO_DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Optional(ATTR_VIDEO_QUALITY, default=DEFAULT_VIDEO_QUALITY): vol.In(VIDEO_QUALITIES),
        vol.Optional(ATTR_VIDEO_FORMAT, default=DEFAULT_VIDEO_FORMAT): vol.In(VIDEO_FORMATS),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

AUDIO_DOWNLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Optional(ATTR_AUDIO_FORMAT, default=DEFAULT_AUDIO_FORMAT): vol.In(AUDIO_FORMATS),
        vol.Optional(ATTR_AUDIO_QUALITY, default=DEFAULT_AUDIO_QUALITY): vol.In(AUDIO_QUALITIES),
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_COMPLETION, default=False): cv.boolean,
    }
)

GET_JOB_SCHEMA = vol.Schema({vol.Required(ATTR_JOB_ID): vol.Match(r"^[0-9a-f]{32}$")})

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_QUERY): search_query,
        vol.Optional(ATTR_LIMIT, default=DEFAULT_SEARCH_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SEARCH_LIMIT)
        ),
    }
)


def async_register_download_services(hass: HomeAssistant) -> None:
    """Register download/search services without importing playback/Favorites."""

    async def _run_download(
        call: ServiceCall, request: DownloadRequest
    ) -> ServiceResponse | None:
        manager = get_loaded_manager(hass)
        try:
            job = await manager.async_start_download(request)
            if call.data[ATTR_WAIT_FOR_COMPLETION]:
                response = await manager.async_wait_for_job(job)
            elif call.return_response:
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
        manager = get_loaded_manager(hass)
        response = manager.get_job_response(call.data[ATTR_JOB_ID])
        if response is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="job_not_found",
                translation_placeholders={"job_id": call.data[ATTR_JOB_ID]},
            )
        return response

    async def async_search(call: ServiceCall) -> ServiceResponse:
        manager = get_loaded_manager(hass)
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
