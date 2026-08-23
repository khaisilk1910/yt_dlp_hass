"""Media Source platform for the configured YouTube-DLP music library."""

from __future__ import annotations

import asyncio
import logging
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN
_LOGGER = logging.getLogger(__name__)
_DLNA_PREPARE_TIMEOUT_SECONDS = 75

from .playback import (
    MEDIA_URL_PREFIX,
    STREAM_MEDIA_SOURCE_PREFIX,
    STREAM_URL_PREFIX,
    _mime_from_suffix,
)


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
        """Resolve one library item to the integration's playback HTTP endpoint."""
        from . import get_playback_manager

        relative = unquote(item.identifier)
        manager = get_playback_manager(self.hass)

        if relative.startswith(STREAM_MEDIA_SOURCE_PREFIX):
            token = relative.removeprefix(STREAM_MEDIA_SOURCE_PREFIX)
            if not token or "/" in token:
                raise Unresolvable("Invalid playback stream")
            stream_session = manager.get_stream_session(token)
            if stream_session is None:
                raise Unresolvable("Playback stream has expired")

            # DLNA is an optional compatibility path. Build a complete MP3 with
            # byte ranges + DLNA headers when possible, but never let DLNA
            # preparation failure take down the original direct playback path.
            if (
                stream_session.info.mime_type.startswith("audio/")
                and _is_dlna_target(self.hass, item.target_media_player)
            ):
                try:
                    from .dlna_runtime import get_dlna_manager

                    dlna = get_dlna_manager(self.hass)
                    async with asyncio.timeout(_DLNA_PREPARE_TIMEOUT_SECONDS):
                        dlna_path = await dlna.async_prepare_remote(manager, token)
                except Exception as err:  # noqa: BLE001 - direct relay fallback is intentional
                    _LOGGER.warning(
                        "DLNA MP3 preparation failed; falling back to original stream: %s",
                        err,
                    )
                else:
                    return PlayMedia(dlna_path, "audio/mpeg")

            # This is an unguessable capability URL. The harmless query string
            # deliberately prevents Home Assistant from appending authSig, which
            # can make Cast media URLs unnecessarily long. A filename suffix also
            # helps strict receivers infer the container before the first bytes.
            stream_path = (
                f"{STREAM_URL_PREFIX}/{token}"
                f"{_stream_suffix(stream_session.info.mime_type)}?source=yt_dlp"
            )

            # MediaSourceItem tells us the actual target. Cast devices sometimes
            # cannot reach HA's detected LAN URL (VLAN/client isolation is common),
            # while the configured external or Home Assistant Cloud URL is usable.
            # Prefer that public route only for Cast; otherwise keep the local path.
            if _is_cast_target(self.hass, item.target_media_player):
                try:
                    base_url = get_url(
                        self.hass,
                        prefer_external=True,
                        prefer_cloud=True,
                    )
                except NoURLAvailableError:
                    pass
                else:
                    return PlayMedia(
                        f"{base_url.rstrip('/')}{stream_path}",
                        stream_session.info.mime_type,
                    )

            return PlayMedia(stream_path, stream_session.info.mime_type)

        path = await self.hass.async_add_executor_job(
            manager.resolve_library_file, relative
        )
        if path is None:
            raise Unresolvable("Media file does not exist")

        mime = mimetypes.guess_type(path.name)[0] or _mime_from_suffix(path.suffix)
        if _is_dlna_target(self.hass, item.target_media_player):
            try:
                from .dlna_runtime import get_dlna_manager

                dlna = get_dlna_manager(self.hass)
                async with asyncio.timeout(_DLNA_PREPARE_TIMEOUT_SECONDS):
                    dlna_path = await dlna.async_prepare_local(path)
            except Exception as err:  # noqa: BLE001 - original-file fallback is intentional
                _LOGGER.warning(
                    "DLNA local-file conversion failed; using original media: %s", err
                )
            else:
                return PlayMedia(dlna_path, "audio/mpeg")

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


def _is_cast_target(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return whether a Media Source request targets Home Assistant Cast."""
    if not entity_id:
        return False
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == "cast"


def _is_dlna_target(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return whether this request should use the DLNA audio compatibility path.

    A configured user classification wins over platform auto-detection. This is
    important for DLNA-capable TVs: when classified as TV they must receive a
    video stream rather than an MP3 conversion. Legacy/unconfigured service
    calls keep v0.5.16 behavior by falling back to the HA entity platform.
    """
    if not entity_id:
        return False
    from .const import TARGET_TYPE_DLNA
    from .media_targets import configured_target

    configured = configured_target(hass, entity_id)
    if configured is not None:
        return configured.target_type == TARGET_TYPE_DLNA
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == "dlna_dmr"


def _stream_suffix(mime_type: str) -> str:
    """Return a cosmetic extension for receivers that inspect the URL path."""
    base = mime_type.split(";", 1)[0].strip().lower()
    return {
        "audio/mp4": ".m4a",
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/flac": ".flac",
        "audio/wav": ".wav",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }.get(base, ".media")
