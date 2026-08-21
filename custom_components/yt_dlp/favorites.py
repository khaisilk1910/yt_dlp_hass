"""Persistent favorite YouTube items for the bundled media card."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_KEY = "yt_dlp.favorites"
STORAGE_VERSION = 1
MAX_FAVORITES = 500


class FavoritesStore:
    """Lazily load and persist favorite metadata in Home Assistant storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            serialize_in_event_loop=False,
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._items: list[dict[str, Any]] = []

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            data = await self._store.async_load()
            raw_items = data.get("items", []) if isinstance(data, dict) else []
            if isinstance(raw_items, list):
                self._items = [
                    dict(item)
                    for item in raw_items
                    if isinstance(item, dict) and isinstance(item.get("url"), str)
                ][:MAX_FAVORITES]
            self._loaded = True

    async def async_list(self) -> list[dict[str, Any]]:
        """Return a detached snapshot so callers cannot mutate stored state."""
        await self._async_ensure_loaded()
        async with self._lock:
            return deepcopy(self._items)

    async def async_add(self, item: dict[str, Any]) -> dict[str, Any]:
        """Add or refresh a favorite, keeping newest items first."""
        await self._async_ensure_loaded()
        url = str(item.get("url") or "").strip()
        if not url:
            raise ValueError("Favorite URL is missing")

        clean = {
            "url": url,
            "title": str(item.get("title") or "YouTube audio"),
            "thumbnail": str(item.get("thumbnail")) if item.get("thumbnail") else None,
            "artist": str(item.get("artist")) if item.get("artist") else None,
            "duration": item.get("duration"),
            "mime_type": str(item.get("mime_type")) if item.get("mime_type") else None,
        }
        async with self._lock:
            self._items = [entry for entry in self._items if entry.get("url") != url]
            self._items.insert(0, clean)
            del self._items[MAX_FAVORITES:]
            snapshot = {"items": deepcopy(self._items)}
            await self._store.async_save(snapshot)
            return deepcopy(clean)

    async def async_remove(self, url: str) -> bool:
        """Remove a favorite by canonical webpage URL."""
        await self._async_ensure_loaded()
        normalized = url.strip()
        async with self._lock:
            before = len(self._items)
            self._items = [entry for entry in self._items if entry.get("url") != normalized]
            changed = len(self._items) != before
            if changed:
                await self._store.async_save({"items": deepcopy(self._items)})
            return changed
