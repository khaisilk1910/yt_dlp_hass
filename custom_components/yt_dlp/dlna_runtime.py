"""Runtime lookup for the optional DLNA compatibility manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN

if TYPE_CHECKING:
    from .dlna import DlnaPlaybackManager


def get_dlna_manager(hass: HomeAssistant) -> DlnaPlaybackManager:
    """Return the loaded optional DLNA manager without touching core services."""
    from .dlna import DlnaPlaybackManager

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
