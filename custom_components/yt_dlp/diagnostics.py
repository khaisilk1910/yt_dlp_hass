"""Privacy-conscious, non-blocking diagnostics for YouTube-DLP.

Home Assistant calls this only when an administrator downloads diagnostics.
This function deliberately performs no filesystem checks, YouTube extraction,
upstream probes or calls to external services, and never includes media URLs,
stream tokens, title/artist, absolute paths, or notification credentials.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant import __version__ as HA_VERSION
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MEDIA_LIBRARY_PATH,
    CONF_MEDIA_TARGETS,
    CONF_NOTIFY_ENABLED,
    DOMAIN,
    SECTION_NOTIFY_HOME_ASSISTANT,
    SECTION_NOTIFY_MOBILE,
    SECTION_NOTIFY_ZALO,
    VERSION,
)


def _notification_state(options: dict[str, Any], section: str) -> bool:
    """Expose enablement, never destination IDs or authentication details."""
    value = options.get(section)
    return bool(value.get(CONF_NOTIFY_ENABLED)) if isinstance(value, dict) else False


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a sanitized runtime snapshot without blocking the HA event loop."""
    runtime = getattr(entry, "runtime_data", None)
    playback = getattr(runtime, "playback_manager", None)
    dlna = getattr(runtime, "dlna_manager", None)
    favorites = getattr(runtime, "favorites_playback", None)
    jobs = getattr(runtime, "jobs", None)
    statuses = Counter(job.status for job in jobs.values()) if isinstance(jobs, dict) else Counter()
    raw_targets = entry.options.get(CONF_MEDIA_TARGETS, [])

    return {
        "integration": {
            "domain": DOMAIN,
            "version": VERSION,
            "home_assistant_version": HA_VERSION,
            "entry_state": str(getattr(entry.state, "value", entry.state)),
        },
        "configuration": {
            "media_library_separately_configured": CONF_MEDIA_LIBRARY_PATH in entry.data,
            "target_count": len(raw_targets) if isinstance(raw_targets, list) else 0,
            "notifications": {
                "home_assistant": _notification_state(entry.options, SECTION_NOTIFY_HOME_ASSISTANT),
                "mobile": _notification_state(entry.options, SECTION_NOTIFY_MOBILE),
                "zalo": _notification_state(entry.options, SECTION_NOTIFY_ZALO),
            },
        },
        "runtime": {
            "core_manager_ready": runtime is not None,
            "post_start": getattr(runtime, "optional_runtime_status", None),
            "active_downloads": sum(count for status, count in statuses.items() if status not in ("completed", "error", "cancelled")),
            "download_job_status_counts": dict(sorted(statuses.items())),
            "playback_manager_ready": playback is not None,
            "stream_sessions": len(getattr(playback, "_stream_sessions", {})) if playback is not None else 0,
            "library_cached": getattr(playback, "_library_cache", None) is not None if playback is not None else False,
            "dlna_manager_ready": dlna is not None,
            "dlna_cached_items": len(getattr(dlna, "_items_by_token", {})) if dlna is not None else 0,
            "favorites_controller_ready": favorites is not None,
        },
    }
