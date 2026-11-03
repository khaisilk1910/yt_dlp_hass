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
    DOMAIN,
    SERVICE_PLAY,
    SERVICE_PLAY_MULTI,
    SERVICE_SCAN_LIBRARY,
)
from .media_targets import TARGET_TYPE_DLNA, platform_for_entity, target_type_for_entity
from .target_services import async_prepare_dlna_stream, async_play_prepared_dlna
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

    async def _async_play_online(url: str, entity_ids: list[str], context):
        """Preserve regular Media Source playback; route DLNA through LAN MP3."""
        playback = get_playback_manager(hass)
        info, media_source_id = await playback.async_create_stream(url)
        metadata: dict[str, object] = {"title": info.title}
        if info.artist:
            metadata["artist"] = info.artist
        if info.thumbnail:
            metadata["images"] = [{"url": info.thumbnail}]

        dlna_ids = [
            entity_id for entity_id in entity_ids
            if platform_for_entity(hass, entity_id) == "dlna_dmr"
            or target_type_for_entity(hass, entity_id) == TARGET_TYPE_DLNA
        ]
        regular_ids = [entity_id for entity_id in entity_ids if entity_id not in dlna_ids]
        if regular_ids:
            await hass.services.async_call(
                "media_player",
                SERVICE_PLAY_MEDIA,
                service_data={
                    ATTR_MEDIA_CONTENT_ID: media_source_id,
                    ATTR_MEDIA_CONTENT_TYPE: info.mime_type,
                    ATTR_MEDIA_EXTRA: {"metadata": metadata},
                },
                target={"entity_id": regular_ids},
                blocking=True,
                context=context,
            )
            for entity_id in regular_ids:
                playback.async_track_remote_playback(entity_id, url, info, media_source_id)

        if dlna_ids:
            relay_path = await async_prepare_dlna_stream(hass, playback, media_source_id)
            for entity_id in dlna_ids:
                await async_play_prepared_dlna(
                    hass,
                    entity_id,
                    relay_path=relay_path,
                    title=info.title,
                    artist=info.artist,
                    context=context,
                )
                playback.async_track_remote_playback(entity_id, url, info, media_source_id)
        return info

    async def async_play(call: ServiceCall) -> ServiceResponse | None:
        entity_id = call.data[ATTR_MEDIA_PLAYER]
        try:
            info = await _async_play_online(call.data[ATTR_URL], [entity_id], call.context)
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
        entity_ids = call.data[ATTR_MEDIA_PLAYERS]
        try:
            info = await _async_play_online(call.data[ATTR_URL], entity_ids, call.context)
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
