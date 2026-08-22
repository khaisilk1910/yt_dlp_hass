"""Favorites-only service boundary.

All persistence/queue features live here and in favorites_playback.py.  This
module may consume the public PlaybackManager API, but it never modifies or
wraps the protected direct play/download service handlers.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_DIRECTION,
    ATTR_KIND,
    ATTR_MEDIA_PLAYERS,
    ATTR_OFFLINE_SELECTED,
    ATTR_ONLINE_SELECTED,
    ATTR_QUEUE,
    ATTR_REPEAT_MODE,
    ATTR_REPLACE_SELECTION,
    ATTR_URL,
    DOMAIN,
    SERVICE_FAVORITES_ADD,
    SERVICE_FAVORITES_LIST,
    SERVICE_FAVORITES_PLAYBACK_GET,
    SERVICE_FAVORITES_PLAYBACK_SET,
    SERVICE_FAVORITES_PLAYBACK_SKIP,
    SERVICE_FAVORITES_PLAYBACK_START,
    SERVICE_FAVORITES_PLAYBACK_STOP,
    SERVICE_FAVORITES_REMOVE,
)
from .favorites_runtime import (
    get_favorites_playback_controller,
    get_favorites_store,
)
from .play_runtime import get_playback_manager
from .service_validation import favorite_keys, http_url, media_player_entities

FAVORITE_URL_SCHEMA = vol.Schema({vol.Required(ATTR_URL): http_url})

FAVORITES_PLAYBACK_SET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ONLINE_SELECTED): favorite_keys,
        vol.Required(ATTR_OFFLINE_SELECTED): favorite_keys,
        vol.Required(ATTR_REPEAT_MODE): vol.In(("off", "one", "all")),
    }
)

FAVORITES_PLAYBACK_START_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KIND): vol.In(("online", "offline")),
        vol.Required(ATTR_QUEUE): favorite_keys,
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
        vol.Required(ATTR_REPEAT_MODE): vol.In(("off", "one", "all")),
        vol.Optional(ATTR_REPLACE_SELECTION, default=True): cv.boolean,
    }
)

FAVORITES_PLAYBACK_SKIP_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DIRECTION): vol.In(("previous", "next", "current"))}
)


def async_register_favorites_services(hass: HomeAssistant) -> None:
    """Register Favorites persistence/queue services only."""

    async def async_favorites_list(call: ServiceCall) -> ServiceResponse:
        items = await get_favorites_store(hass).async_list()
        return {"count": len(items), "items": items}

    async def async_favorites_add(call: ServiceCall) -> ServiceResponse:
        playback = get_playback_manager(hass)
        store = get_favorites_store(hass)
        try:
            info = await playback.async_resolve_stream(call.data[ATTR_URL])
            item = await store.async_add(info.as_dict())
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="favorite_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return {"item": item}

    async def async_favorites_remove(call: ServiceCall) -> ServiceResponse:
        url = call.data[ATTR_URL]
        removed = await get_favorites_store(hass).async_remove(url)
        if removed:
            await get_favorites_playback_controller(hass).async_remove_online_key(url)
        return {"removed": removed}

    async def async_favorites_playback_get(call: ServiceCall) -> ServiceResponse:
        return await get_favorites_playback_controller(hass).async_get_state()

    async def async_favorites_playback_set(call: ServiceCall) -> ServiceResponse:
        return await get_favorites_playback_controller(hass).async_update_settings(
            online_selected=call.data[ATTR_ONLINE_SELECTED],
            offline_selected=call.data[ATTR_OFFLINE_SELECTED],
            repeat_mode=call.data[ATTR_REPEAT_MODE],
        )

    async def async_favorites_playback_start(call: ServiceCall) -> ServiceResponse:
        try:
            return await get_favorites_playback_controller(hass).async_start_queue(
                kind=call.data[ATTR_KIND],
                queue=call.data[ATTR_QUEUE],
                media_players=call.data[ATTR_MEDIA_PLAYERS],
                repeat_mode=call.data[ATTR_REPEAT_MODE],
                replace_selection=call.data[ATTR_REPLACE_SELECTION],
            )
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_favorites_playback_skip(call: ServiceCall) -> ServiceResponse:
        try:
            return await get_favorites_playback_controller(hass).async_skip(
                call.data[ATTR_DIRECTION]
            )
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_favorites_playback_stop(call: ServiceCall) -> ServiceResponse:
        return await get_favorites_playback_controller(hass).async_stop_playback(
            clear_selection=True
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_LIST,
        async_favorites_list,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_ADD,
        async_favorites_add,
        schema=FAVORITE_URL_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_REMOVE,
        async_favorites_remove,
        schema=FAVORITE_URL_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_GET,
        async_favorites_playback_get,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_SET,
        async_favorites_playback_set,
        schema=FAVORITES_PLAYBACK_SET_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_START,
        async_favorites_playback_start,
        schema=FAVORITES_PLAYBACK_START_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_SKIP,
        async_favorites_playback_skip,
        schema=FAVORITES_PLAYBACK_SKIP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_STOP,
        async_favorites_playback_stop,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
