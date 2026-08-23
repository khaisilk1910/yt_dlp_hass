"""Protected speaker/local-library service boundary.

This module never imports the download worker or Favorites controller.  It is
the only service layer allowed to turn a remote URL into media_player playback.
"""

from __future__ import annotations

import voluptuous as vol

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
    SERVICE_GET_MEDIA_TARGETS,
)
from .play_runtime import get_playback_manager
from .media_targets import configured_media_targets
from .target_playback import async_play_url_on_targets
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
GET_MEDIA_TARGETS_SCHEMA = vol.Schema({})


def async_register_play_services(hass: HomeAssistant) -> None:
    """Register protected direct speaker playback and library scan services."""

    async def async_play(call: ServiceCall) -> ServiceResponse | None:
        playback = get_playback_manager(hass)
        entity_id = call.data[ATTR_MEDIA_PLAYER]
        try:
            info, results = await async_play_url_on_targets(
                hass,
                playback,
                call.data[ATTR_URL],
                [entity_id],
                context=call.context,
            )
            result = results[0] if results else None
            if result is None or not result.success:
                raise RuntimeError(result.error if result else "Playback target returned no result")
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        response: dict[str, object] = {
            "media_player": entity_id,
            "target": result.as_dict(),
            "url": call.data[ATTR_URL],
        }
        if info is not None:
            response.update(info.as_dict())
        return response if call.return_response else None

    async def async_play_multi(call: ServiceCall) -> ServiceResponse | None:
        playback = get_playback_manager(hass)
        entity_ids = call.data[ATTR_MEDIA_PLAYERS]
        try:
            info, results = await async_play_url_on_targets(
                hass,
                playback,
                call.data[ATTR_URL],
                list(entity_ids),
                context=call.context,
            )
            successes = [result for result in results if result.success]
            if not successes:
                errors = "; ".join(result.error or result.entity_id for result in results)
                raise RuntimeError(errors or "No playback target succeeded")
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        response: dict[str, object] = {
            "media_players": entity_ids,
            "player_count": len(entity_ids),
            "success_count": len(successes),
            "failed_count": len(results) - len(successes),
            "targets": [result.as_dict() for result in results],
            "url": call.data[ATTR_URL],
        }
        if info is not None:
            response.update(info.as_dict())
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

    async def async_get_media_targets(call: ServiceCall) -> ServiceResponse:
        """Return only user-managed targets; this performs no network discovery."""
        targets = configured_media_targets(hass)
        return {
            "count": len(targets),
            "targets": [target.as_dict() for target in targets],
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
        SERVICE_GET_MEDIA_TARGETS,
        async_get_media_targets,
        schema=GET_MEDIA_TARGETS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_LIBRARY,
        async_scan_library,
        schema=SCAN_LIBRARY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
