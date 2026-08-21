"""Authenticated/signed HTTP endpoint for configured YouTube-DLP library files."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant

from .playback import _mime_from_suffix


class YoutubeDlpMediaView(HomeAssistantView):
    """Serve library audio through Home Assistant signed media URLs."""

    url = "/api/yt_dlp/media/{location:.*}"
    name = "api:yt_dlp:media"
    requires_auth = True

    async def _path(self, hass: HomeAssistant, location: str) -> Path:
        from . import get_playback_manager

        manager = get_playback_manager(hass)
        path = await hass.async_add_executor_job(
            manager.resolve_library_file, location
        )
        if path is None:
            raise web.HTTPNotFound
        return path

    async def head(self, request: web.Request, location: str) -> web.Response:
        """Return media headers for renderers that probe with HEAD first."""
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, location)
        mime = mimetypes.guess_type(path.name)[0] or _mime_from_suffix(path.suffix)
        return web.Response(content_type=mime)

    async def get(self, request: web.Request, location: str) -> web.FileResponse:
        """Stream a local media file with aiohttp range support."""
        hass: HomeAssistant = request.app[KEY_HASS]
        path = await self._path(hass, location)
        return web.FileResponse(path)
