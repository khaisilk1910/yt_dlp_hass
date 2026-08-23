"""Runtime lookup for DLNA compatibility playback."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .dlna import DlnaPlaybackManager


def get_dlna_manager(hass: HomeAssistant) -> DlnaPlaybackManager:
    """Return the loaded DLNA compatibility manager."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        manager = getattr(entry.runtime_data, "dlna_manager", None)
        if isinstance(manager, DlnaPlaybackManager):
            return manager
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )
