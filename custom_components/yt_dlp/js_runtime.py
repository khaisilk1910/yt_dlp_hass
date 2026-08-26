"""Lazy JavaScript runtime preparation for yt-dlp.

The large Node.js wheel is intentionally *not* a manifest requirement. Home
Assistant processes manifest requirements before an integration can load; a
transient package-install failure would therefore leave the config entry Not
loaded.  Current yt-dlp only needs the JS runtime when a YouTube extraction is
actually requested, so install/resolve it on first Play/Download/Search instead.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.requirements import (
    async_load_installed_versions,
    async_process_requirements,
)

from .const import DOMAIN
from .helpers import detect_javascript_runtime

_LOGGER = logging.getLogger(__name__)

NODE_REQUIREMENT: Final = "nodejs-wheel-binaries==24.19.0"
_DATA_LOCK: Final = f"{DOMAIN}_javascript_runtime_lock"
_DATA_RUNTIME: Final = f"{DOMAIN}_javascript_runtime"


async def async_ensure_javascript_runtime(
    hass: HomeAssistant,
) -> tuple[str, str]:
    """Return a supported yt-dlp JS runtime, installing Node lazily if needed.

    This function is only called from user-triggered media operations. It never
    runs from ``async_setup``/``async_setup_entry`` and therefore cannot delay or
    fail Home Assistant startup.
    """
    cached = hass.data.get(_DATA_RUNTIME)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached

    runtime = await hass.async_add_executor_job(detect_javascript_runtime)
    if runtime is not None:
        hass.data[_DATA_RUNTIME] = runtime
        return runtime

    lock = hass.data.get(_DATA_LOCK)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        hass.data[_DATA_LOCK] = lock

    async with lock:
        cached = hass.data.get(_DATA_RUNTIME)
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached

        runtime = await hass.async_add_executor_job(detect_javascript_runtime)
        if runtime is not None:
            hass.data[_DATA_RUNTIME] = runtime
            return runtime

        _LOGGER.info(
            "No supported JavaScript runtime is available; installing %s lazily",
            NODE_REQUIREMENT,
        )
        try:
            # Populate HA's requirement cache first. If a previous release
            # already installed the wheel, this avoids any pip/network work.
            await async_load_installed_versions(hass, {NODE_REQUIREMENT})
            await async_process_requirements(
                hass,
                f"{DOMAIN}_javascript_runtime",
                [NODE_REQUIREMENT],
                is_built_in=False,
            )
        except Exception as err:  # noqa: BLE001 - convert to feature-level failure
            raise RuntimeError(
                "YouTube JavaScript runtime is unavailable. "
                "Home Assistant could not install the optional Node.js runtime: "
                f"{err}"
            ) from err

        # A package may have been installed while this interpreter was already
        # running. Invalidate import caches before resolving its bundled binary.
        await hass.async_add_executor_job(importlib.invalidate_caches)
        runtime = await hass.async_add_executor_job(detect_javascript_runtime)
        if runtime is None:
            raise RuntimeError(
                "Node.js runtime installation completed but no supported "
                "yt-dlp JavaScript runtime could be resolved"
            )

        hass.data[_DATA_RUNTIME] = runtime
        _LOGGER.info("YouTube-DLP JavaScript runtime ready: %s", runtime[0])
        return runtime
