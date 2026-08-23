"""Optional managed-target services.

The protected v0.5.16 ``play`` / ``play_multi`` / download services do not
import this module. Managed target playback is registered after the core and is
allowed to fail independently.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import voluptuous as vol

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ATTR_MEDIA_PLAYERS,
    ATTR_URL,
    DOMAIN,
    SERVICE_GET_MEDIA_TARGETS,
    SERVICE_PLAY_TARGETS,
    TARGET_TYPE_TV,
)
from .media_targets import configured_media_targets, platform_for_entity, target_type_for_entity
from .play_runtime import get_playback_manager
from .service_validation import http_url, media_player_entities
from .tv_playback import get_tv_manager

_GET_SCHEMA = vol.Schema({})
_PLAY_TARGETS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
    }
)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _youtube_video_id(url: str) -> str | None:
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
    return candidate if candidate and _YOUTUBE_ID_RE.fullmatch(candidate) else None


async def _play_media(
    hass: HomeAssistant,
    entity_id: str | list[str],
    *,
    media_id: str,
    media_type: str,
    metadata: dict[str, object] | None,
    context,
) -> None:
    data: dict[str, object] = {
        ATTR_MEDIA_CONTENT_ID: media_id,
        ATTR_MEDIA_CONTENT_TYPE: media_type,
    }
    if metadata:
        data[ATTR_MEDIA_EXTRA] = {"metadata": metadata}
    await hass.services.async_call(
        "media_player",
        SERVICE_PLAY_MEDIA,
        service_data=data,
        target={"entity_id": entity_id},
        blocking=True,
        context=context,
    )


def async_register_target_services(hass: HomeAssistant) -> None:
    """Register optional managed target discovery/playback services."""

    async def async_get_targets(call: ServiceCall) -> ServiceResponse:
        targets = configured_media_targets(hass)
        return {"count": len(targets), "targets": [target.as_dict() for target in targets]}

    async def async_play_targets(call: ServiceCall) -> ServiceResponse:
        url = call.data[ATTR_URL]
        entity_ids = list(call.data[ATTR_MEDIA_PLAYERS])
        playback = get_playback_manager(hass)
        results: list[dict[str, object]] = []

        audio_targets = [
            entity_id
            for entity_id in entity_ids
            if target_type_for_entity(hass, entity_id) != TARGET_TYPE_TV
        ]
        tv_targets = [
            entity_id
            for entity_id in entity_ids
            if target_type_for_entity(hass, entity_id) == TARGET_TYPE_TV
        ]

        representative: dict[str, object] = {}

        # Exact v0.5.16 audio creation + media_player.play_media behavior. This
        # code is optional; the original yt_dlp.play service remains untouched.
        if audio_targets:
            try:
                info, media_source_id = await playback.async_create_stream(url)
                metadata: dict[str, object] = {"title": info.title}
                if info.artist:
                    metadata["artist"] = info.artist
                if info.thumbnail:
                    metadata["images"] = [{"url": info.thumbnail}]
                await _play_media(
                    hass,
                    audio_targets if len(audio_targets) > 1 else audio_targets[0],
                    media_id=media_source_id,
                    media_type=info.mime_type,
                    metadata=metadata,
                    context=call.context,
                )
                for entity_id in audio_targets:
                    playback.async_track_remote_playback(entity_id, url, info, media_source_id)
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": target_type_for_entity(hass, entity_id),
                            "method": "v0516_audio",
                            "success": True,
                            "error": None,
                        }
                    )
                representative.update(info.as_dict())
            except Exception as err:  # noqa: BLE001 - isolate target failures
                for entity_id in audio_targets:
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": target_type_for_entity(hass, entity_id),
                            "method": "v0516_audio",
                            "success": False,
                            "error": str(err),
                        }
                    )

        video_id = _youtube_video_id(url)
        tv_payload: tuple[object, str] | None = None
        tv_error: Exception | None = None
        for entity_id in tv_targets:
            platform = platform_for_entity(hass, entity_id)
            if platform == "cast" and video_id:
                try:
                    await _play_media(
                        hass,
                        entity_id,
                        media_id=json.dumps(
                            {"app_name": "youtube", "media_id": video_id},
                            separators=(",", ":"),
                        ),
                        media_type="cast",
                        metadata=None,
                        context=call.context,
                    )
                except Exception as err:  # noqa: BLE001 - generic TV fallback below
                    native_error = err
                else:
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": TARGET_TYPE_TV,
                            "method": "youtube_native_cast",
                            "success": True,
                            "error": None,
                        }
                    )
                    continue
            else:
                native_error = None

            try:
                if tv_error is not None:
                    raise tv_error
                if tv_payload is None:
                    tv_payload = await get_tv_manager(hass).async_create_stream(url)
                info, relay_path = tv_payload
                relay_url = async_process_play_media_url(hass, relay_path)
                metadata = {"title": info.title}
                if info.thumbnail:
                    metadata["images"] = [{"url": info.thumbnail}]
                await _play_media(
                    hass,
                    entity_id,
                    media_id=relay_url,
                    media_type=info.mime_type,
                    metadata=metadata,
                    context=call.context,
                )
                results.append(
                    {
                        "entity_id": entity_id,
                        "type": TARGET_TYPE_TV,
                        "method": "video_relay",
                        "success": True,
                        "error": None,
                    }
                )
                if not representative:
                    representative.update(info.as_dict())
            except Exception as err:  # noqa: BLE001
                tv_error = tv_error or err
                combined = str(err)
                if native_error is not None:
                    combined = f"native cast: {native_error}; video relay: {err}"
                results.append(
                    {
                        "entity_id": entity_id,
                        "type": TARGET_TYPE_TV,
                        "method": "video_relay",
                        "success": False,
                        "error": combined,
                    }
                )

        success_count = sum(1 for result in results if result["success"])
        if success_count == 0:
            errors = "; ".join(str(result.get("error") or result["entity_id"]) for result in results)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": errors or "No target succeeded"},
            )

        return {
            "url": url,
            "media_players": entity_ids,
            "player_count": len(entity_ids),
            "success_count": success_count,
            "failed_count": len(results) - success_count,
            "targets": results,
            **representative,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MEDIA_TARGETS,
        async_get_targets,
        schema=_GET_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_TARGETS,
        async_play_targets,
        schema=_PLAY_TARGETS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
