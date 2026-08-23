"""Playback dispatch for user-managed speakers, DLNA renderers and TVs.

The dispatcher is deliberately service-time only: it performs no discovery and
starts no background work.  Configuration merely stores entity IDs, labels and
user-selected target classes; playback then chooses the cheapest compatible
path for each target.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.core import Context, HomeAssistant

from .const import TARGET_TYPE_DLNA, TARGET_TYPE_SPEAKER, TARGET_TYPE_TV
from .media_targets import platform_for_entity, target_type_for_entity
from .playback import PlaybackManager, StreamInfo

_LOGGER = logging.getLogger(__name__)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(slots=True)
class TargetPlaybackResult:
    """Result for one requested media_player target."""

    entity_id: str
    target_type: str
    method: str
    success: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "type": self.target_type,
            "method": self.method,
            "success": self.success,
            "error": self.error,
        }


def youtube_video_id(url: str) -> str | None:
    """Extract a canonical YouTube video ID without any network request."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    if candidate and _YOUTUBE_ID_RE.fullmatch(candidate):
        return candidate
    return None


def _metadata(info: StreamInfo) -> dict[str, object]:
    metadata: dict[str, object] = {"title": info.title}
    if info.artist:
        metadata["artist"] = info.artist
    if info.thumbnail:
        metadata["images"] = [{"url": info.thumbnail}]
    return metadata


async def _async_call_play_media(
    hass: HomeAssistant,
    entity_id: str,
    *,
    media_content_id: str,
    media_content_type: str,
    context: Context | None,
    metadata: dict[str, object] | None = None,
) -> None:
    service_data: dict[str, Any] = {
        ATTR_MEDIA_CONTENT_ID: media_content_id,
        ATTR_MEDIA_CONTENT_TYPE: media_content_type,
    }
    if metadata:
        service_data[ATTR_MEDIA_EXTRA] = {"metadata": metadata}
    await hass.services.async_call(
        "media_player",
        SERVICE_PLAY_MEDIA,
        service_data=service_data,
        target={"entity_id": entity_id},
        blocking=True,
        context=context,
    )


async def async_play_url_on_targets(
    hass: HomeAssistant,
    playback: PlaybackManager,
    url: str,
    entity_ids: list[str],
    *,
    context: Context | None = None,
) -> tuple[StreamInfo | None, list[TargetPlaybackResult]]:
    """Play one YouTube URL using a target-specific strategy.

    Audio and video streams are each resolved at most once for the entire call,
    so mixed multi-room playback does not repeat yt-dlp extraction. Chromecast
    TVs take the native YouTube-app route first and avoid extraction entirely.
    """
    audio_payload: tuple[StreamInfo, str] | None = None
    video_payload: tuple[StreamInfo, str] | None = None
    audio_error: Exception | None = None
    video_error: Exception | None = None
    video_id = youtube_video_id(url)
    representative: StreamInfo | None = None
    results: list[TargetPlaybackResult] = []

    for entity_id in entity_ids:
        target_type = target_type_for_entity(hass, entity_id)
        try:
            if target_type == TARGET_TYPE_TV:
                platform = platform_for_entity(hass, entity_id)
                if platform == "cast" and video_id:
                    try:
                        await _async_call_play_media(
                            hass,
                            entity_id,
                            media_content_id=json.dumps(
                                {"app_name": "youtube", "media_id": video_id},
                                separators=(",", ":"),
                            ),
                            media_content_type="cast",
                            context=context,
                        )
                    except Exception as err:  # noqa: BLE001 - fallback is intentional
                        _LOGGER.warning(
                            "Native YouTube Cast failed for %s; falling back to MP4 relay: %s",
                            entity_id,
                            err,
                        )
                    else:
                        results.append(
                            TargetPlaybackResult(
                                entity_id, target_type, "youtube_native_cast", True
                            )
                        )
                        continue

                if video_error is not None:
                    raise video_error
                if video_payload is None:
                    try:
                        video_payload = await playback.async_create_video_stream(url)
                    except Exception as err:  # noqa: BLE001 - share one failed resolution
                        video_error = err
                        raise
                info, media_source_id = video_payload
                representative = representative or info
                await _async_call_play_media(
                    hass,
                    entity_id,
                    media_content_id=media_source_id,
                    media_content_type=info.mime_type,
                    metadata=_metadata(info),
                    context=context,
                )
                results.append(
                    TargetPlaybackResult(entity_id, target_type, "video_relay", True)
                )
                continue

            # Both regular speakers and user-classified DLNA speakers start from
            # the stable v0.5.16 audio path. Media Source applies DLNA MP3
            # compatibility only for the DLNA target when resolving the token.
            if target_type not in {TARGET_TYPE_SPEAKER, TARGET_TYPE_DLNA}:
                target_type = TARGET_TYPE_SPEAKER
            if audio_error is not None:
                raise audio_error
            if audio_payload is None:
                try:
                    audio_payload = await playback.async_create_stream(url)
                except Exception as err:  # noqa: BLE001 - share one failed resolution
                    audio_error = err
                    raise
            info, media_source_id = audio_payload
            representative = representative or info
            await _async_call_play_media(
                hass,
                entity_id,
                media_content_id=media_source_id,
                media_content_type=info.mime_type,
                metadata=_metadata(info),
                context=context,
            )
            playback.async_track_remote_playback(entity_id, url, info, media_source_id)
            results.append(
                TargetPlaybackResult(
                    entity_id,
                    target_type,
                    "dlna_audio" if target_type == TARGET_TYPE_DLNA else "audio_relay",
                    True,
                )
            )
        except Exception as err:  # noqa: BLE001 - multi-target playback is fault-isolated
            _LOGGER.warning("Playback failed for target %s: %s", entity_id, err)
            results.append(
                TargetPlaybackResult(entity_id, target_type, "failed", False, str(err))
            )

    return representative, results
