"""Configured Home Assistant playback targets for YouTube-DLP.

Target discovery is intentionally in-memory only.  We never scan the network or
perform I/O during integration startup; the options flow reads the media_player
entities Home Assistant already knows about and stores only the user's choices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_MEDIA_TARGETS,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_NAME,
    CONF_TARGET_TYPE,
    DOMAIN,
    TARGET_TYPE_DLNA,
    TARGET_TYPE_SPEAKER,
    TARGET_TYPE_TV,
    TARGET_TYPES,
)

_TV_PLATFORMS = frozenset(
    {
        "androidtv",
        "androidtv_remote",
        "apple_tv",
        "braviatv",
        "kodi",
        "roku",
        "samsungtv",
        "webostv",
    }
)


@dataclass(slots=True, frozen=True)
class MediaTarget:
    """One user-managed playback target."""

    entity_id: str
    name: str
    target_type: str
    platform: str | None
    available: bool
    state: str | None
    supported_features: int

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for the bundled dashboard card."""
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "type": self.target_type,
            "platform": self.platform,
            "available": self.available,
            "state": self.state,
            "supported_features": self.supported_features,
        }


def _loaded_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the single loaded YouTube-DLP entry without doing I/O."""
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            return entry
    return entries[0] if entries else None


def raw_media_targets(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return normalized target records stored in ConfigEntry.options."""
    value = entry.options.get(CONF_MEDIA_TARGETS, [])
    if not isinstance(value, list):
        return []

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get(CONF_TARGET_ENTITY_ID) or "").strip()
        if not entity_id.startswith("media_player.") or entity_id in seen:
            continue
        target_type = str(item.get(CONF_TARGET_TYPE) or TARGET_TYPE_SPEAKER)
        if target_type not in TARGET_TYPES:
            target_type = TARGET_TYPE_SPEAKER
        name = str(item.get(CONF_TARGET_NAME) or "").strip()
        seen.add(entity_id)
        result.append(
            {
                CONF_TARGET_ENTITY_ID: entity_id,
                CONF_TARGET_NAME: name,
                CONF_TARGET_TYPE: target_type,
            }
        )
    return result


def configured_media_targets(hass: HomeAssistant) -> list[MediaTarget]:
    """Return configured targets enriched with current HA state/registry data."""
    entry = _loaded_entry(hass)
    if entry is None:
        return []
    registry = er.async_get(hass)
    result: list[MediaTarget] = []
    for item in raw_media_targets(entry):
        entity_id = item[CONF_TARGET_ENTITY_ID]
        state = hass.states.get(entity_id)
        registry_entry = registry.async_get(entity_id)
        friendly_name = (
            str(state.attributes.get("friendly_name") or "").strip()
            if state is not None
            else ""
        )
        try:
            supported_features = int(
                state.attributes.get("supported_features") or 0
            ) if state is not None else 0
        except (TypeError, ValueError):
            supported_features = 0
        result.append(
            MediaTarget(
                entity_id=entity_id,
                name=item[CONF_TARGET_NAME] or friendly_name or entity_id,
                target_type=item[CONF_TARGET_TYPE],
                platform=registry_entry.platform if registry_entry is not None else None,
                available=state is not None and state.state not in {"unavailable", "unknown"},
                state=state.state if state is not None else None,
                supported_features=supported_features,
            )
        )
    return result


def configured_target(hass: HomeAssistant, entity_id: str) -> MediaTarget | None:
    """Return one configured target by entity_id."""
    return next(
        (target for target in configured_media_targets(hass) if target.entity_id == entity_id),
        None,
    )


def target_type_for_entity(hass: HomeAssistant, entity_id: str) -> str:
    """Return configured target type, preserving v0.5.16 speaker compatibility."""
    target = configured_target(hass, entity_id)
    return target.target_type if target is not None else TARGET_TYPE_SPEAKER


def platform_for_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the integration platform behind one media_player entity."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.platform if entry is not None else None


def guess_target_type(hass: HomeAssistant, entity_id: str) -> str:
    """Suggest a target type from HA metadata; the user can always override it."""
    platform = platform_for_entity(hass, entity_id)
    if platform == "dlna_dmr":
        return TARGET_TYPE_DLNA
    state = hass.states.get(entity_id)
    device_class = str(state.attributes.get("device_class") or "").lower() if state else ""
    if device_class == "tv" or platform in _TV_PLATFORMS:
        return TARGET_TYPE_TV
    return TARGET_TYPE_SPEAKER


def media_player_candidates(hass: HomeAssistant) -> list[str]:
    """Return HA's known media players, PLAY_MEDIA-capable entities first.

    This is a zero-I/O state-machine lookup used only when the user opens the
    options flow.  Keeping entities without a currently advertised PLAY_MEDIA
    bit is intentional because some TV/DLNA integrations expose fewer features
    while powered off and restore them after waking.
    """
    preferred: list[tuple[str, str]] = []
    fallback: list[tuple[str, str]] = []
    for state in hass.states.async_all():
        if not state.entity_id.startswith("media_player."):
            continue
        friendly = str(state.attributes.get("friendly_name") or state.entity_id)
        try:
            features = int(state.attributes.get("supported_features") or 0)
        except (TypeError, ValueError):
            features = 0
        item = (friendly.casefold(), state.entity_id)
        if features & int(MediaPlayerEntityFeature.PLAY_MEDIA):
            preferred.append(item)
        else:
            fallback.append(item)
    return [entity_id for _, entity_id in sorted(preferred) + sorted(fallback)]
