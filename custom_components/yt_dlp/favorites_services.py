"""Favorites-only service boundary.

All persistence/queue features live here and in favorites_playback.py.  This
module may consume the public PlaybackManager API, but it never modifies or
wraps the protected direct play/download service handlers.
"""

from __future__ import annotations

import random

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_DIRECTION,
    ATTR_KIND,
    ATTR_MEDIA_PLAYERS,
    ATTR_OFFLINE_SELECTED,
    ATTR_PLAY_MODE,
    ATTR_POSITION,
    ATTR_ONLINE_SELECTED,
    ATTR_QUEUE,
    ATTR_REPEAT_MODE,
    ATTR_REPLACE_SELECTION,
    ATTR_URL,
    DOMAIN,
    SERVICE_FAVORITES_ADD,
    SERVICE_FAVORITES_LIST,
    SERVICE_FAVORITES_PLAYBACK_GET,
    SERVICE_FAVORITES_PLAYBACK_PLAY_POSITION,
    SERVICE_FAVORITES_PLAYBACK_PLAY_RANDOM,
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

# A separate automation-friendly action; the original queue action is unchanged.
# Position is 1-based in the same order as the card's Online/Offline tabs.
FAVORITES_PLAYBACK_PLAY_POSITION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KIND): vol.In(("online", "offline")),
        vol.Required(ATTR_POSITION): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
        vol.Optional(ATTR_PLAY_MODE, default="single"): vol.In(("single", "continue")),
        vol.Optional(ATTR_REPEAT_MODE, default="off"): vol.In(("off", "one", "all")),
        vol.Optional(ATTR_REPLACE_SELECTION, default=False): cv.boolean,
    }
)

# Draw a fresh single track on every action call; no queue or position needed.
# No filesystem or network work is performed by this module at setup time.
FAVORITES_PLAYBACK_PLAY_RANDOM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KIND): vol.In(("online", "offline")),
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
        vol.Optional(ATTR_REPEAT_MODE, default="off"): vol.In(("off", "one", "all")),
        vol.Optional(ATTR_REPLACE_SELECTION, default=False): cv.boolean,
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

    async def async_favorites_playback_play_position(call: ServiceCall) -> ServiceResponse | None:
        """Play a catalog item by its 1-based visual position, without manual IDs.

        Online uses the stored Favorites ordering (newest first); Offline uses
        the cached library scan ordering (newest files first). No I/O happens
        until this action is invoked, so integration startup stays unchanged.
        """
        kind = call.data[ATTR_KIND]
        position = call.data[ATTR_POSITION]
        try:
            if kind == "online":
                items = await get_favorites_store(hass).async_list()
                keys = favorite_keys(
                    [item["url"] for item in items if isinstance(item.get("url"), str)]
                )
            else:
                items = await get_playback_manager(hass).async_scan_library(force=False)
                keys = favorite_keys(
                    [item["id"] for item in items if isinstance(item.get("id"), str)]
                )
            if position > len(keys):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="favorite_position_invalid",
                    translation_placeholders={
                        "position": str(position), "count": str(len(keys))
                    },
                )

            play_mode = call.data[ATTR_PLAY_MODE]
            queue = [keys[position - 1]] if play_mode == "single" else keys
            state = await get_favorites_playback_controller(hass).async_start_queue(
                kind=kind,
                queue=queue,
                media_players=call.data[ATTR_MEDIA_PLAYERS],
                repeat_mode=call.data[ATTR_REPEAT_MODE],
                replace_selection=call.data[ATTR_REPLACE_SELECTION],
                start_index=0 if play_mode == "single" else position - 1,
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # Optional response is essential for regular automations/scripts: they
        # must be able to play music without declaring a response_variable.
        if not call.return_response:
            return None
        return {
            **state,
            "selected_position": position,
            "selected_key": keys[position - 1],
            "total_items": len(keys),
        }

    async def async_favorites_playback_play_random(call: ServiceCall) -> ServiceResponse | None:
        """Pick exactly one random Favorite on demand and play it.

        A fresh choice is made for every invocation. The checked Favorites in
        the dashboard are not changed unless explicitly requested. No extra
        work is scheduled on the Home Assistant startup path.
        """
        kind = call.data[ATTR_KIND]
        try:
            if kind == "online":
                items = await get_favorites_store(hass).async_list()
                keys = favorite_keys(
                    [item["url"] for item in items
                     if isinstance(item, dict) and isinstance(item.get("url"), str)]
                )
            else:
                items = await get_playback_manager(hass).async_scan_library(force=False)
                keys = favorite_keys(
                    [item["id"] for item in items
                     if isinstance(item, dict) and isinstance(item.get("id"), str)]
                )
            if not keys:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="favorite_random_empty",
                    translation_placeholders={"kind": kind},
                )

            selected_key = random.choice(keys)
            state = await get_favorites_playback_controller(hass).async_start_queue(
                kind=kind,
                queue=[selected_key],
                start_index=0,
                media_players=call.data[ATTR_MEDIA_PLAYERS],
                repeat_mode=call.data[ATTR_REPEAT_MODE],
                replace_selection=call.data[ATTR_REPLACE_SELECTION],
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        if not call.return_response:
            return None
        return {
            **state,
            "selected_position": keys.index(selected_key) + 1,
            "selected_key": selected_key,
            "total_items": len(keys),
        }

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
        SERVICE_FAVORITES_PLAYBACK_PLAY_POSITION,
        async_favorites_playback_play_position,
        schema=FAVORITES_PLAYBACK_PLAY_POSITION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FAVORITES_PLAYBACK_PLAY_RANDOM,
        async_favorites_playback_play_random,
        schema=FAVORITES_PLAYBACK_PLAY_RANDOM_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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
