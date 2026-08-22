"""Runtime lookup for the protected speaker playback boundary only."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .playback import PlaybackManager


def get_playback_manager(hass: HomeAssistant) -> PlaybackManager:
    """Return playback runtime without importing the download/Favorites engines."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        playback = getattr(entry.runtime_data, "playback_manager", None)
        if isinstance(playback, PlaybackManager):
            return playback
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )
