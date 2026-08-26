"""Optional managed-target services.

The protected v0.5.16 ``play`` / ``play_multi`` / download services do not
import this module. Managed target playback is registered after the core and is
allowed to fail independently.

DLNA playback intentionally has its own path.  Home Assistant's dlna_dmr
integration resolves a URL, probes it with HEAD/GET to build DIDL-Lite, sends
SetAVTransportURI and finally Play.  For strict renderers we therefore expose a
short LAN-reachable MP3 capability URL with stable Content-Length/Range/DLNA
headers instead of handing the renderer a YouTube/GoogleVideo URL directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
import yarl

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_TYPE, CONF_URL
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    ATTR_MEDIA_PLAYERS,
    ATTR_URL,
    DOMAIN,
    SERVICE_GET_MEDIA_TARGETS,
    SERVICE_PLAY_TARGETS,
    TARGET_TYPE_DLNA,
    TARGET_TYPE_SPEAKER,
    TARGET_TYPE_TV,
)
from .media_targets import configured_media_targets, platform_for_entity, target_type_for_entity
from .play_runtime import get_playback_manager
from .playback import STREAM_MEDIA_SOURCE_PREFIX
from .service_validation import http_url, media_player_entities

_LOGGER = logging.getLogger(__name__)
_GET_SCHEMA = vol.Schema({})
_PLAY_TARGETS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): http_url,
        vol.Required(ATTR_MEDIA_PLAYERS): media_player_entities,
    }
)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_STREAM_MEDIA_SOURCE_ROOT = f"media-source://{DOMAIN}/{STREAM_MEDIA_SOURCE_PREFIX}"
_DLNA_PREPARE_TIMEOUT_SECONDS = 120
_DLNA_START_VERIFY_SECONDS = 3
_DLNA_DIRECT_TIMEOUT_SECONDS = 15
_DLNA_MUSIC_UPNP_CLASS = "object.item.audioItem.musicTrack"


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


def _stream_token(media_source_id: str) -> str:
    if not media_source_id.startswith(_STREAM_MEDIA_SOURCE_ROOT):
        raise RuntimeError("Unexpected playback media-source id")
    token = media_source_id.removeprefix(_STREAM_MEDIA_SOURCE_ROOT)
    if not token or "/" in token:
        raise RuntimeError("Invalid playback stream token")
    return token


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


def _dlna_config_entry(hass: HomeAssistant, entity_id: str):
    """Return the Home Assistant dlna_dmr config entry for an entity."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.platform != "dlna_dmr":
        return None
    if not registry_entry.config_entry_id:
        return None
    return hass.config_entries.async_get_entry(registry_entry.config_entry_id)


async def _async_dlna_device_location(
    hass: HomeAssistant, entity_id: str
) -> str | None:
    """Return the freshest UPnP description URL Home Assistant knows.

    The config entry stores the last-known URL, but a renderer can receive a new
    DHCP address without that persisted URL changing.  Home Assistant's SSDP
    scanner keeps the latest LOCATION in memory, so prefer that cache and fall
    back to the config entry.  This performs no network discovery.
    """
    config_entry = _dlna_config_entry(hass, entity_id)
    if config_entry is None:
        return None

    fallback = config_entry.data.get(CONF_URL)
    udn = str(config_entry.data.get(CONF_DEVICE_ID) or "").strip()
    st = str(config_entry.data.get(CONF_TYPE) or "").strip()
    if udn:
        try:
            from homeassistant.components import ssdp

            if st:
                info = await ssdp.async_get_discovery_info_by_udn_st(hass, udn, st)
                if info is not None and info.ssdp_location:
                    return str(info.ssdp_location)
            for info in await ssdp.async_get_discovery_info_by_udn(hass, udn):
                if info.ssdp_location:
                    return str(info.ssdp_location)
        except Exception as err:  # noqa: BLE001 - cached-location fallback below
            _LOGGER.debug("Could not read SSDP cache for %s: %s", entity_id, err)

    return str(fallback) if fallback else None


async def _dlna_base_url(hass: HomeAssistant, entity_id: str) -> str:
    """Return a URL the renderer can normally reach on the same LAN.

    For a real Home Assistant dlna_dmr entity, use the same routing decision HA
    uses for UPnP event callbacks: ask async_upnp_client which local interface
    reaches the renderer. This avoids external/cloud URLs, DNS/mDNS problems and
    the common case where a renderer cannot validate HTTPS certificates.
    """
    api = hass.config.api
    location = await _async_dlna_device_location(hass, entity_id)
    if location and api is not None and not api.use_ssl:
        try:
            # Lazy import: the optional managed-target layer must not make
            # async_upnp_client a startup dependency for non-DLNA users.
            from async_upnp_client.utils import async_get_local_ip

            _target_ip, local_ip = await async_get_local_ip(location, hass.loop)
            if local_ip:
                return str(
                    yarl.URL.build(
                        scheme="http",
                        host=local_ip,
                        port=api.port,
                    )
                ).rstrip("/")
        except Exception as err:  # noqa: BLE001 - safe HA URL fallback below
            _LOGGER.debug(
                "Could not derive target-specific DLNA LAN URL for %s: %s",
                entity_id,
                err,
            )

    try:
        return get_url(
            hass,
            allow_internal=True,
            allow_external=False,
            allow_cloud=False,
            allow_ip=True,
            prefer_external=False,
        ).rstrip("/")
    except NoURLAvailableError:
        # Last-resort HA behavior. This can use an external URL but is preferable
        # to failing before the renderer gets a chance to fetch the resource.
        return get_url(hass).rstrip("/")


async def _dlna_media_url(hass: HomeAssistant, entity_id: str, relay_path: str) -> str:
    """Build a short target-specific capability URL for a prepared DLNA file."""
    base_url = await _dlna_base_url(hass, entity_id)
    return f"{base_url}{relay_path}"


def _target_is_available(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state not in {"unavailable", "unknown"}


async def _async_wait_dlna_started(
    hass: HomeAssistant, entity_id: str, relay_url: str
) -> bool:
    """Wait briefly for HA to observe the renderer accepting the URI/play command."""
    relay_path = yarl.URL(relay_url).path
    deadline = hass.loop.time() + _DLNA_START_VERIFY_SECONDS
    while hass.loop.time() < deadline:
        state = hass.states.get(entity_id)
        if state is None or state.state in {"unavailable", "unknown"}:
            return False
        if state.state in {"playing", "paused"}:
            return True
        current_media = state.attributes.get("media_content_id")
        if current_media:
            try:
                if yarl.URL(str(current_media)).path == relay_path:
                    return True
            except ValueError:
                pass
        await asyncio.sleep(0.25)
    return False


async def _async_direct_dlna_play(
    hass: HomeAssistant,
    entity_id: str,
    *,
    relay_url: str,
    title: str,
    artist: str | None,
) -> None:
    """One-shot AVTransport fallback using HA's own async_upnp_client stack.

    This is only used when the regular Home Assistant dlna_dmr entity is
    unavailable or accepted the service call without observable playback.
    It does not subscribe to events or create background tasks.
    """
    location = await _async_dlna_device_location(hass, entity_id)
    if not location:
        raise RuntimeError("No DLNA DMR location is available for direct fallback")

    # Keep these imports strictly on the DLNA button-press path. An ordinary
    # speaker or download must never depend on Home Assistant's DLNA internals.
    from async_upnp_client.exceptions import UpnpError
    from async_upnp_client.profiles.dlna import DmrDevice
    from homeassistant.components.dlna_dmr.data import get_domain_data

    from .dlna import DLNA_MIME_TYPE

    domain_data = get_domain_data(hass)
    upnp_device = await domain_data.upnp_factory.async_create_device(location)
    device = DmrDevice(upnp_device, None)
    await device.async_update(do_ping=True)

    meta_data: dict[str, str] = {}
    if artist:
        meta_data["artist"] = artist
    # Match Home Assistant/async_upnp_client behavior: let the DMR client HEAD/GET
    # the relay and reconcile ContentFeatures with ConnectionManager protocol info.
    # We only force the known MIME and musicTrack class.
    didl = await device.construct_play_media_metadata(
        media_url=relay_url,
        media_title=title or "Home Assistant",
        override_mime_type=DLNA_MIME_TYPE,
        override_upnp_class=_DLNA_MUSIC_UPNP_CLASS,
        meta_data=meta_data,
    )

    if device.can_stop:
        await device.async_stop()

    try:
        await device.async_set_transport_uri(relay_url, title or "Home Assistant", didl)
    except UpnpError as first_err:
        # Some embedded DMRs accept MP3 but reject a concrete DLNA profile. Try
        # protocolInfo with wildcard features before the final URI-only fallback.
        try:
            conservative_didl = await device.construct_play_media_metadata(
                media_url=relay_url,
                media_title=title or "Home Assistant",
                override_mime_type=DLNA_MIME_TYPE,
                override_upnp_class=_DLNA_MUSIC_UPNP_CLASS,
                override_dlna_features="*",
                meta_data=meta_data,
            )
            await device.async_set_transport_uri(
                relay_url, title or "Home Assistant", conservative_didl
            )
        except UpnpError as second_err:
            _LOGGER.debug(
                "DLNA renderer %s rejected DIDL metadata (%s; %s), retrying URI-only",
                entity_id,
                first_err,
                second_err,
            )
            await device.async_set_transport_uri(
                relay_url, title or "Home Assistant", ""
            )

    await device.async_wait_for_can_play(max_wait_time=5)
    await device.async_play()



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

        speaker_targets = [
            entity_id
            for entity_id in entity_ids
            if target_type_for_entity(hass, entity_id) == TARGET_TYPE_SPEAKER
        ]
        dlna_targets = [
            entity_id
            for entity_id in entity_ids
            if target_type_for_entity(hass, entity_id) == TARGET_TYPE_DLNA
        ]
        tv_targets = [
            entity_id
            for entity_id in entity_ids
            if target_type_for_entity(hass, entity_id) == TARGET_TYPE_TV
        ]

        representative: dict[str, object] = {}
        audio_payload: tuple[object, str] | None = None
        audio_error: Exception | None = None

        async def async_audio_payload():
            nonlocal audio_payload, audio_error
            if audio_error is not None:
                raise audio_error
            if audio_payload is None:
                try:
                    audio_payload = await playback.async_create_stream(url)
                except Exception as err:  # noqa: BLE001
                    audio_error = err
                    raise
            return audio_payload

        # Regular speakers stay on the proven v0.5.16 media-source path.
        if speaker_targets:
            try:
                info, media_source_id = await async_audio_payload()
                metadata: dict[str, object] = {"title": info.title}
                if info.artist:
                    metadata["artist"] = info.artist
                if info.thumbnail:
                    metadata["images"] = [{"url": info.thumbnail}]
                await _play_media(
                    hass,
                    speaker_targets if len(speaker_targets) > 1 else speaker_targets[0],
                    media_id=media_source_id,
                    media_type=info.mime_type,
                    metadata=metadata,
                    context=call.context,
                )
                for entity_id in speaker_targets:
                    playback.async_track_remote_playback(entity_id, url, info, media_source_id)
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": TARGET_TYPE_SPEAKER,
                            "method": "v0516_audio",
                            "success": True,
                            "error": None,
                        }
                    )
                representative.update(info.as_dict())
            except Exception as err:  # noqa: BLE001 - isolate target failures
                for entity_id in speaker_targets:
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": TARGET_TYPE_SPEAKER,
                            "method": "v0516_audio",
                            "success": False,
                            "error": str(err),
                        }
                    )

        # DLNA is intentionally per-renderer. Each DMR gets a URL whose host is
        # on the interface used to reach that renderer. Home Assistant gets the
        # first attempt so its entity/state remains authoritative; if HA cannot
        # maintain a DMR connection, a bounded one-shot AVTransport fallback
        # uses the same async_upnp_client stack without background subscriptions.
        if dlna_targets:
            try:
                info, media_source_id = await async_audio_payload()
                from .dlna_runtime import get_dlna_manager

                stream_token = _stream_token(media_source_id)
                async with asyncio.timeout(_DLNA_PREPARE_TIMEOUT_SECONDS):
                    relay_path = await get_dlna_manager(hass).async_prepare_remote(
                        playback, stream_token
                    )
                # Keep DIDL metadata intentionally small. A number of strict
                # renderers reject vendor/unknown fields; title + artist are
                # standard properties for object.item.audioItem.musicTrack.
                metadata = {"title": info.title}
                if info.artist:
                    metadata["artist"] = info.artist

                for entity_id in dlna_targets:
                    try:
                        relay_url = await _dlna_media_url(hass, entity_id, relay_path)
                        ha_ready = _target_is_available(hass, entity_id)
                        method = "dlna_ha_dmr"
                        started = False

                        # Never send a Home Assistant service target for an entity
                        # that HA currently marks unavailable/missing.  The service
                        # helper rejects that target before dlna_dmr can run, which
                        # is exactly the "Referenced entities ... are missing or not
                        # currently available" error.  Use AVTransport directly in
                        # that state instead.
                        if ha_ready:
                            try:
                                await _play_media(
                                    hass,
                                    entity_id,
                                    media_id=relay_url,
                                    # ``music`` makes HA dlna_dmr use musicTrack
                                    # while HEAD/GET discovers audio/mpeg.
                                    media_type=MediaType.MUSIC,
                                    metadata=metadata,
                                    context=call.context,
                                )
                            except Exception as err:  # noqa: BLE001 - direct fallback
                                _LOGGER.debug(
                                    "HA dlna_dmr service path failed for %s: %s",
                                    entity_id,
                                    err,
                                )
                            else:
                                started = await _async_wait_dlna_started(
                                    hass, entity_id, relay_url
                                )

                        if not started:
                            location = await _async_dlna_device_location(hass, entity_id)
                            if location is None:
                                if ha_ready:
                                    # A user may classify another media_player
                                    # platform as DLNA.  Only its own HA service can
                                    # control it because there is no DMR description.
                                    method = "dlna_platform_mp3"
                                else:
                                    raise RuntimeError(
                                        "DLNA renderer is unavailable and no UPnP "
                                        "description URL is known"
                                    )
                            else:
                                async with asyncio.timeout(_DLNA_DIRECT_TIMEOUT_SECONDS):
                                    await _async_direct_dlna_play(
                                        hass,
                                        entity_id,
                                        relay_url=relay_url,
                                        title=info.title,
                                        artist=info.artist,
                                    )
                                method = (
                                    "dlna_direct_unavailable"
                                    if not ha_ready
                                    else "dlna_direct_fallback"
                                )

                        playback.async_track_remote_playback(
                            entity_id, url, info, media_source_id
                        )
                        results.append(
                            {
                                "entity_id": entity_id,
                                "type": TARGET_TYPE_DLNA,
                                "method": method,
                                "success": True,
                                "error": None,
                            }
                        )
                    except Exception as err:  # noqa: BLE001 - per-renderer isolation
                        results.append(
                            {
                                "entity_id": entity_id,
                                "type": TARGET_TYPE_DLNA,
                                "method": "dlna_lan_mp3",
                                "success": False,
                                "error": str(err),
                            }
                        )
                if not representative:
                    representative.update(info.as_dict())
            except Exception as err:  # noqa: BLE001 - DLNA preparation isolation
                for entity_id in dlna_targets:
                    if any(result["entity_id"] == entity_id for result in results):
                        continue
                    results.append(
                        {
                            "entity_id": entity_id,
                            "type": TARGET_TYPE_DLNA,
                            "method": "dlna_lan_mp3",
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
                    # TV is optional and must never be an import-time dependency of
                    # managed speaker/DLNA playback. Import only when a TV is
                    # actually selected.
                    from .tv_playback import get_tv_manager

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
            errors = "; ".join(
                str(result.get("error") or result["entity_id"]) for result in results
            )
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
