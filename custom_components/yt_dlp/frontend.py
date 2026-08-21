"""Frontend registration for the bundled YouTube-DLP media card."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
CARD_URL = "/yt_dlp_static/yt-dlp-media-card.js"
DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"
DATA_EXTRA_MODULE = f"{DOMAIN}_extra_module"


async def async_register_media_card(hass: HomeAssistant, version: str) -> None:
    """Serve the card and add/update its Lovelace resource with cache busting."""
    card_path = Path(__file__).parent / "frontend" / "yt-dlp-media-card.js"
    if not await hass.async_add_executor_job(card_path.is_file):
        _LOGGER.warning("YouTube-DLP media card bundle is missing: %s", card_path)
        return

    versioned_url = f"{CARD_URL}?v={version}"
    try:
        if not hass.data.get(DATA_STATIC_REGISTERED):
            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_URL, str(card_path), True)]
            )
            hass.data[DATA_STATIC_REGISTERED] = True

        lovelace = hass.data.get(LOVELACE_DATA)
        if lovelace is None:
            _register_extra_module(hass, versioned_url)
            return

        resources = lovelace.resources
        if isinstance(resources, ResourceStorageCollection):
            # Current HA lazily loads resource storage. async_get_info is the
            # guarded public path and avoids touching an unloaded collection.
            await resources.async_get_info()
            matches = [
                item
                for item in resources.async_items()
                if str(item.get("url") or "").startswith(CARD_URL)
            ]
            if matches:
                primary = matches[0]
                if (
                    str(primary.get("url") or "") != versioned_url
                    or primary.get("type") != "module"
                ):
                    await resources.async_update_item(
                        primary["id"],
                        {"res_type": "module", "url": versioned_url},
                    )
                    _LOGGER.info(
                        "Updated YouTube-DLP Lovelace card resource to %s",
                        versioned_url,
                    )

                # Old releases or manual additions may have left duplicates.
                # Keep exactly one managed resource so every update has one URL.
                for duplicate in matches[1:]:
                    await resources.async_delete_item(duplicate["id"])
                return

            await resources.async_create_item(
                {"res_type": "module", "url": versioned_url}
            )
            _LOGGER.info(
                "Registered YouTube-DLP Lovelace card resource: %s",
                versioned_url,
            )
            return

        # YAML resources cannot be edited from an integration. Home Assistant's
        # supported extra-module registry keeps the card automatic in that mode.
        _register_extra_module(hass, versioned_url)
    except Exception:
        # Frontend convenience must never make the downloader integration fail.
        # The task is deliberately backgrounded by async_setup_entry as well.
        _LOGGER.exception(
            "Unable to register YouTube-DLP media card automatically; "
            "download and playback services remain available"
        )


def _register_extra_module(hass: HomeAssistant, versioned_url: str) -> None:
    """Replace our previous extra-module URL so reloads do not accumulate versions."""
    previous = hass.data.get(DATA_EXTRA_MODULE)
    if isinstance(previous, str) and previous != versioned_url:
        remove_extra_js_url(hass, previous)
    if previous != versioned_url:
        add_extra_js_url(hass, versioned_url)
        hass.data[DATA_EXTRA_MODULE] = versioned_url
