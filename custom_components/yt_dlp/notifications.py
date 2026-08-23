"""Completion notifications for YouTube-DLP downloads.

Notification delivery only uses Home Assistant's in-memory service registry and
async service calls. It performs no network or filesystem work during integration
setup, so enabling this module cannot delay Home Assistant startup.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_MOBILE_NOTIFY_ACTION,
    CONF_NOTIFY_ENABLED,
    CONF_ZALO_ACCOUNT,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    MEDIA_TYPE_AUDIO,
    SECTION_NOTIFY_HOME_ASSISTANT,
    SECTION_NOTIFY_MOBILE,
    SECTION_NOTIFY_ZALO,
    ZALO_SERVICE_DOMAIN,
    ZALO_SERVICE_SEND_MESSAGE,
    ZALO_TYPE_GROUP,
)

_LOGGER = logging.getLogger(__name__)


def mobile_notify_actions(hass: HomeAssistant) -> list[str]:
    """Return registered Companion App notify actions from HA's service registry."""
    notify_services = hass.services.async_services().get("notify", {})
    return sorted(
        f"notify.{service}"
        for service in notify_services
        if service.startswith("mobile_app_")
    )


def mobile_notify_action_label(action: str) -> str:
    """Return a compact human-readable label for a mobile notify action."""
    service = action.partition(".")[2] if "." in action else action
    device = service.removeprefix("mobile_app_").replace("_", " ").strip()
    friendly = device.title() if device else service
    return f"{friendly} ({action})"


async def async_send_download_notifications(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Send all enabled completion notifications without affecting job success."""
    if result.get("status") != "completed":
        return

    home_cfg = _section(options, SECTION_NOTIFY_HOME_ASSISTANT)
    mobile_cfg = _section(options, SECTION_NOTIFY_MOBILE)
    zalo_cfg = _section(options, SECTION_NOTIFY_ZALO)

    if home_cfg.get(CONF_NOTIFY_ENABLED, False):
        title, message = _home_assistant_message(result)
        await _async_send_home_assistant(hass, title, message)

    if mobile_cfg.get(CONF_NOTIFY_ENABLED, False):
        action = str(mobile_cfg.get(CONF_MOBILE_NOTIFY_ACTION) or "").strip()
        if action:
            title, message = _mobile_message(result)
            await _async_call_action(
                hass,
                action,
                {"title": title, "message": message},
                channel="mobile",
            )
        else:
            _LOGGER.warning(
                "YouTube-DLP mobile notification is enabled but no mobile "
                "action is configured"
            )

    if zalo_cfg.get(CONF_NOTIFY_ENABLED, False):
        thread_id = str(zalo_cfg.get(CONF_ZALO_THREAD_ID) or "").strip()
        account = str(zalo_cfg.get(CONF_ZALO_ACCOUNT) or "").strip()
        target_type = str(zalo_cfg.get(CONF_ZALO_TYPE) or "user").strip()
        if thread_id and account:
            await _async_call_action(
                hass,
                f"{ZALO_SERVICE_DOMAIN}.{ZALO_SERVICE_SEND_MESSAGE}",
                {
                    "account_selection": account,
                    "thread_id": thread_id,
                    "message": _zalo_message(result),
                    "type": 1 if target_type == ZALO_TYPE_GROUP else 0,
                },
                channel="Zalo",
            )
        else:
            _LOGGER.warning(
                "YouTube-DLP Zalo notification is enabled but account/thread "
                "ID is incomplete"
            )


def _section(options: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = options.get(key)
    return value if isinstance(value, Mapping) else {}


async def _async_send_home_assistant(
    hass: HomeAssistant, title: str, message: str
) -> None:
    """Send a persistent notification, preferring the current notify action."""
    if hass.services.has_service("notify", "persistent_notification"):
        await _async_call_action(
            hass,
            "notify.persistent_notification",
            {"title": title, "message": message},
            channel="Home Assistant",
        )
        return

    # Fallback keeps compatibility with installations where the notify building
    # block is not loaded but the persistent_notification integration is present.
    if hass.services.has_service("persistent_notification", "create"):
        await _async_call_action(
            hass,
            "persistent_notification.create",
            {"title": title, "message": message},
            channel="Home Assistant",
        )
        return

    _LOGGER.warning(
        "YouTube-DLP Home Assistant notification is enabled but no persistent "
        "notification action is available"
    )


async def _async_call_action(
    hass: HomeAssistant,
    action: str,
    data: dict[str, Any],
    *,
    channel: str,
) -> None:
    """Call a Home Assistant action non-blockingly and isolate delivery failures."""
    domain, dot, service = action.partition(".")
    if not dot or not domain or not service:
        _LOGGER.warning(
            "Invalid YouTube-DLP %s notification action: %s", channel, action
        )
        return
    if not hass.services.has_service(domain, service):
        _LOGGER.warning(
            "YouTube-DLP %s notification action %s is not currently available",
            channel,
            action,
        )
        return

    try:
        # blocking=False schedules the target action and returns immediately after
        # HA accepts it. A slow phone/Zalo endpoint therefore cannot delay the
        # download worker or tie up Home Assistant's event loop.
        await hass.services.async_call(domain, service, data, blocking=False)
    except Exception:  # noqa: BLE001 - notification failure must not fail download
        _LOGGER.exception(
            "YouTube-DLP could not dispatch %s notification through %s",
            channel,
            action,
        )


def _home_assistant_message(result: Mapping[str, Any]) -> tuple[str, str]:
    """Build a Markdown-friendly Home Assistant persistent notification."""
    title = "✅ YouTube-DLP • Tải xuống hoàn tất"
    details = _details(result)
    lines = [
        f"### 🎉 {_escape_markdown(details['title'])}",
        "",
        f"- 📄 **Tên file:** {_escape_markdown(details['filename'])}",
        f"- 🎬 **Loại:** {_escape_markdown(details['media_type'])}",
        f"- 📦 **Định dạng:** {_escape_markdown(details['format'])}",
        f"- 🎚️ **Chất lượng:** {_escape_markdown(details['quality'])}",
        f"- 💾 **Dung lượng:** {_escape_markdown(details['size'])}",
    ]
    if details["duration"]:
        lines.append(f"- ⏱️ **Thời lượng:** {_escape_markdown(details['duration'])}")
    if details["resolution"]:
        lines.append(
            f"- 🖼️ **Độ phân giải:** {_escape_markdown(details['resolution'])}"
        )
    if details["channel"]:
        lines.append(f"- 👤 **Kênh:** {_escape_markdown(details['channel'])}")
    if details["path"]:
        lines.append(f"- 📁 **Lưu tại:** {_escape_markdown(details['path'])}")
    if details["source_url"]:
        lines.append(
            f"- 🔗 **Nguồn:** {_escape_markdown(details['source_url'])}"
        )
    lines.extend(("", f"🆔 Job: `{_escape_code(details['job_id'])}`"))
    return title, "\n".join(lines)


def _mobile_message(result: Mapping[str, Any]) -> tuple[str, str]:
    """Build a compact phone push notification."""
    details = _details(result)
    title = "✅ YouTube-DLP • Đã tải xong"
    lines = [
        f"📄 {details['filename']}",
        f"🎬 {details['media_type']} • {details['format']} • {details['quality']}",
        f"💾 {details['size']}"
        + (f" • ⏱️ {details['duration']}" if details["duration"] else ""),
    ]
    if details["channel"]:
        lines.append(f"👤 {details['channel']}")
    if details["path"]:
        lines.append(f"📁 {details['path']}")
    return title, "\n".join(lines)


def _zalo_message(result: Mapping[str, Any]) -> str:
    """Build a plain-text Zalo message with clear visual grouping."""
    details = _details(result)
    lines = [
        "✅ YOUTUBE-DLP • TẢI XONG",
        "━━━━━━━━━━━━",
        f"📝 Tiêu đề: {details['title']}",
        f"📄 Tệp: {details['filename']}",
        f"🎬 Loại: {details['media_type']}",
        f"📦 Định dạng: {details['format']}",
        f"🎚 Chất lượng: {details['quality']}",
        f"💾 Dung lượng: {details['size']}",
    ]
    if details["duration"]:
        lines.append(f"⏱ Thời lượng: {details['duration']}")
    if details["resolution"]:
        lines.append(f"🖼 Độ phân giải: {details['resolution']}")
    if details["channel"]:
        lines.append(f"👤 Kênh: {details['channel']}")
    if details["path"]:
        lines.append(f"📁 Lưu tại: {details['path']}")
    if details["source_url"]:
        lines.append(f"🔗 Nguồn: {details['source_url']}")
    lines.append(f"🆔 Job: {details['job_id']}")
    return "\n".join(lines)


def _details(result: Mapping[str, Any]) -> dict[str, str]:
    """Normalize result values once for all notification formats."""
    media_type = "Âm thanh" if result.get("media_type") == MEDIA_TYPE_AUDIO else "Video"
    filename = _plain(
        result.get("filename") or _first_final_filename(result) or "Không xác định"
    )
    title = _plain(result.get("title") or filename)
    final_format = _plain(
        result.get("format") or _suffix(filename) or "Không xác định"
    ).upper()
    quality = _plain(result.get("quality") or "best")
    if quality.casefold() == "best":
        quality = "Tốt nhất"
    size_value = result.get("file_size_bytes")
    if not isinstance(size_value, (int, float)):
        size_value = result.get("total_bytes")
    size = _format_bytes(size_value)
    duration = _plain(result.get("duration_string") or "")
    resolution = _plain(result.get("resolution") or "")
    channel = _plain(result.get("channel") or result.get("uploader") or "")
    path = _plain(_first_final_path(result) or "")
    source_url = _safe_url(result.get("source_url") or result.get("webpage_url"))
    job_id = _plain(result.get("job_id") or "-")
    return {
        "media_type": media_type,
        "filename": filename,
        "title": title,
        "format": final_format,
        "quality": quality,
        "size": size,
        "duration": duration,
        "resolution": resolution,
        "channel": channel,
        "path": path,
        "source_url": source_url,
        "job_id": job_id,
    }


def _first_final_path(result: Mapping[str, Any]) -> str | None:
    values = result.get("final_files")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value:
                return value
    return None


def _first_final_filename(result: Mapping[str, Any]) -> str | None:
    path = _first_final_path(result)
    return os.path.basename(path) if path else None


def _suffix(filename: str) -> str:
    return os.path.splitext(filename)[1].lstrip(".")


def _plain(value: Any, max_length: int = 500) -> str:
    if value is None:
        text = ""
    else:
        text = (
            str(value)
            .replace("\r", " ")
            .replace("\n", " ")
            .replace("\t", " ")
            .strip()
        )
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"


def _safe_url(value: Any) -> str:
    text = _plain(value, 1200)
    return text if text.startswith(("http://", "https://")) else ""


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for char in ("`", "*", "_", "[", "]", "|", "~"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _escape_code(value: str) -> str:
    return value.replace("`", "\\`")


def _format_bytes(value: Any) -> str:
    try:
        size = max(0.0, float(value))
    except (TypeError, ValueError):
        return "Không xác định"

    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.2f} {units[index]}"
