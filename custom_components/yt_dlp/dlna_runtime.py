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
    """Return or lazily create the optional DLNA manager.

    The normal path initializes this after Home Assistant has fully started. The
    lazy fallback makes service calls robust if an automation fires immediately
    at startup or the post-start convenience task was unable to run.
    """
    from .dlna import DlnaPlaybackManager

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(entry, "runtime_data", None)
        manager = getattr(runtime, "dlna_manager", None)
        if isinstance(manager, DlnaPlaybackManager):
            return manager
        if runtime is not None:
            manager = DlnaPlaybackManager(hass, None)
            runtime.dlna_manager = manager
            return manager
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_configured",
    )
