"""Runtime lookup for Favorites-only objects."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .favorites import FavoritesStore
from .favorites_playback import FavoritesPlaybackController


def _runtime_objects(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            yield entry.runtime_data


def get_favorites_store(hass: HomeAssistant) -> FavoritesStore:
    for runtime in _runtime_objects(hass):
        store = getattr(runtime, "favorites_store", None)
        if isinstance(store, FavoritesStore):
            return store
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )


def get_favorites_playback_controller(
    hass: HomeAssistant,
) -> FavoritesPlaybackController:
    for runtime in _runtime_objects(hass):
        controller = getattr(runtime, "favorites_playback", None)
        if isinstance(controller, FavoritesPlaybackController):
            return controller
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )
