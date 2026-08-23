"""Protected speaker/local-library service boundary.

This module never imports the download worker or Favorites controller.  It is
the only service layer allowed to turn a remote URL into media_player playback.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_FORCE,
    ATTR_MEDIA_PLAYER,
    ATTR_MEDIA_PLAYERS,
    ATTR_URL,
    CONF_MEDIA_TARGETS,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_TYPE,
    DOMAIN,
    SERVICE_PLAY,
    SERVICE_PLAY_MULTI,
    SERVICE_PLAY_TARGETS,
    SERVICE_SCAN_LIBRARY,
    TARGET_TYPE_DLNA,
    TARGET_TYPE_TV,
)
from .play_runtime import get_playback_manager
from .service_validation import http_url, media_player_entities, media_player_entity

PLAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Required(ATTR_MEDIA_PLAYER): media_player_entity,
    }
)

PLAY_MULTI_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
    }
)

SCAN_LIBRARY_SCHEMA = vol.Schema({vol.Optional(ATTR_FORCE, default=False): cv.boolean})


def async_register_play_services(hass: HomeAssistant) -> None:
    """Register protected direct speaker playback and library scan services."""

    def _configured_target_type(entity_id: str) -> str | None:
        # Zero-I/O lookup directly from this integration's config-entry options.
        # Avoid importing optional target modules into the core speaker boundary.
        for entry in hass.config_entries.async_entries(DOMAIN):
            records = entry.options.get(CONF_MEDIA_TARGETS, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                if str(record.get(CONF_TARGET_ENTITY_ID) or "") == entity_id:
                    return str(record.get(CONF_TARGET_TYPE) or "")
        return None

    def _needs_managed_dispatch(entity_ids: list[str]) -> bool:
        # Only configured DLNA/TV devices are diverted; ordinary speakers execute
        # the original v0.5.16 code below unchanged.
        return any(
            _configured_target_type(entity_id) in {TARGET_TYPE_DLNA, TARGET_TYPE_TV}
            for entity_id in entity_ids
        )

    async def _dispatch_managed(
        call: ServiceCall, entity_ids: list[str]
    ) -> ServiceResponse | None:
        if not hass.services.has_service(DOMAIN, SERVICE_PLAY_TARGETS):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={
                    "error": "Managed DLNA/TV playback service is unavailable"
                },
            )
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_PLAY_TARGETS,
            service_data={
                ATTR_URL: call.data[ATTR_URL],
                ATTR_MEDIA_PLAYERS: entity_ids,
            },
            blocking=True,
            context=call.context,
            return_response=True,
        )
        return response if call.return_response else None

    async def async_play(call: ServiceCall) -> ServiceResponse | None:
        entity_id = call.data[ATTR_MEDIA_PLAYER]
        if _needs_managed_dispatch([entity_id]):
            return await _dispatch_managed(call, [entity_id])

        playback = get_playback_manager(hass)
        try:
            info, media_source_id = await playback.async_create_stream(call.data[ATTR_URL])
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
            playback.async_track_remote_playback(
                entity_id, call.data[ATTR_URL], info, media_source_id
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
        entity_ids = list(call.data[ATTR_MEDIA_PLAYERS])
        if _needs_managed_dispatch(entity_ids):
            return await _dispatch_managed(call, entity_ids)

        playback = get_playback_manager(hass)
        try:
            info, media_source_id = await playback.async_create_stream(call.data[ATTR_URL])
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
            for entity_id in entity_ids:
                playback.async_track_remote_playback(
                    entity_id, call.data[ATTR_URL], info, media_source_id
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
