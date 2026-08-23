"""Runtime lookup for the protected download boundary only."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .manager import YoutubeDlpManager


def get_loaded_manager(hass: HomeAssistant) -> YoutubeDlpManager:
    """Return the loaded download manager without importing playback/Favorites."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            manager = entry.runtime_data
            if isinstance(manager, YoutubeDlpManager):
                return manager
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )
