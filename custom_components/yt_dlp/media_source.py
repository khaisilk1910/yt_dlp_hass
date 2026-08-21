"""Media Source platform for the configured YouTube-DLP music library."""

from __future__ import annotations

import mimetypes
from urllib.parse import quote, unquote

from homeassistant.components.media_player import (
    BrowseError,
    MediaClass,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .playback import MEDIA_URL_PREFIX, _mime_from_suffix


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Return the YouTube-DLP media source."""
    return YoutubeDlpMediaSource(hass)


class YoutubeDlpMediaSource(MediaSource):
    """Expose the configured scan folder to Home Assistant's Media Browser."""

    name = "YouTube-DLP Music"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve one library item to the integration's signed HTTP endpoint."""
        from . import get_playback_manager

        relative = unquote(item.identifier)
        manager = get_playback_manager(self.hass)
        path = await self.hass.async_add_executor_job(
            manager.resolve_library_file, relative
        )
        if path is None:
            raise Unresolvable("Media file does not exist")

        mime = mimetypes.guess_type(path.name)[0] or _mime_from_suffix(path.suffix)
        return PlayMedia(
            f"{MEDIA_URL_PREFIX}/{quote(relative, safe='/')}",
            mime,
            path=path,
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the cached/scanned music list as a flat, newest-first library."""
        from . import get_playback_manager

        if item.identifier:
            raise BrowseError("Folders are not exposed by this media source")

        manager = get_playback_manager(self.hass)
        files = await manager.async_scan_library()
        root = BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            title=self.name,
            can_play=False,
            can_expand=True,
            can_search=True,
            children_media_class=MediaClass.MUSIC,
        )
        root.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=str(file["relative_path"]),
                media_class=MediaClass.MUSIC,
                media_content_type=str(file["mime_type"]),
                title=str(file["filename"]),
                can_play=True,
                can_expand=False,
            )
            for file in files
        ]
        return root

    async def async_search_media(
        self, item: MediaSourceItem, query: SearchMediaQuery
    ) -> SearchMedia:
        """Search library filenames without rescanning for each keystroke."""
        from . import get_playback_manager

        manager = get_playback_manager(self.hass)
        files = await manager.async_scan_library()
        needle = query.search_query.casefold()
        results = []
        for file in files:
            if needle not in str(file["filename"]).casefold():
                continue
            results.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=str(file["relative_path"]),
                    media_class=MediaClass.MUSIC,
                    media_content_type=str(file["mime_type"]),
                    title=str(file["filename"]),
                    can_play=True,
                    can_expand=False,
                )
            )
            if len(results) >= 100:
                break
        return SearchMedia(result=results)
