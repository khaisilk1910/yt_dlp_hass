"""Config and options flows for YouTube-DLP."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_FILE_PATH
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_MEDIA_LIBRARY_PATH,
    CONF_MEDIA_TARGETS,
    CONF_MOBILE_NOTIFY_ACTION,
    CONF_NOTIFY_ENABLED,
    CONF_ZALO_ACCOUNT,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    CONF_TARGET_ENTITY_ID,
    CONF_TARGET_NAME,
    CONF_TARGET_TYPE,
    DEFAULT_NOTIFY_ENABLED,
    DOMAIN,
    SECTION_FOLDERS,
    SECTION_NOTIFY_HOME_ASSISTANT,
    SECTION_NOTIFY_MOBILE,
    SECTION_NOTIFY_ZALO,
    TARGET_TYPE_DLNA,
    TARGET_TYPE_SPEAKER,
    TARGET_TYPE_TV,
    ZALO_TYPE_USER,
    ZALO_TYPES,
)
from .helpers import ensure_writable_directory, normalize_download_directory
from .notifications import mobile_notify_action_label, mobile_notify_actions
from .media_targets import guess_target_type, media_player_candidates, raw_media_targets

_BOOLEAN_SELECTOR = BooleanSelector()
_TEXT_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle YouTube-DLP configuration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the unified settings options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the initial media download and library folders."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                path = await self.hass.async_add_executor_job(
                    ensure_writable_directory, user_input[CONF_FILE_PATH]
                )
                library_path = normalize_download_directory(
                    str(user_input.get(CONF_MEDIA_LIBRARY_PATH) or path)
                )
            except (OSError, ValueError):
                errors["base"] = "cannot_create_folder"
            else:
                await self.async_set_unique_id(f"{DOMAIN}.downloader")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="YouTube-DLP",
                    data={
                        CONF_FILE_PATH: path,
                        CONF_MEDIA_LIBRARY_PATH: library_path,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FILE_PATH): str,
                    vol.Optional(CONF_MEDIA_LIBRARY_PATH, default=""): str,
                }
            ),
            errors=errors,
        )


class OptionsFlowHandler(OptionsFlow):
    """Configure folders, notifications and user-managed playback targets."""

    def __init__(self) -> None:
        """Initialize transient options-flow state only."""
        self._pending_target_entity_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a lightweight settings menu without scanning the network."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "media_targets"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage folders, notification targets and enable switches."""
        errors: dict[str, str] = {}
        available_mobile_actions = mobile_notify_actions(self.hass)

        if user_input is not None:
            folders_cfg = _option_section(user_input, SECTION_FOLDERS)
            download_path: str | None = None
            library_path: str | None = None
            current_download = normalize_download_directory(
                self.config_entry.data[CONF_FILE_PATH]
            )
            current_library = normalize_download_directory(
                self.config_entry.data.get(CONF_MEDIA_LIBRARY_PATH, current_download)
            )
            try:
                requested_download = normalize_download_directory(
                    str(
                        folders_cfg.get(CONF_FILE_PATH)
                        or self.config_entry.data[CONF_FILE_PATH]
                    )
                )
                # Do not touch the filesystem for notification-only saves. A
                # writability check runs in an executor only when the download
                # folder itself was changed by the user.
                if requested_download != current_download:
                    download_path = await self.hass.async_add_executor_job(
                        ensure_writable_directory, requested_download
                    )
                else:
                    download_path = current_download

                library_path = normalize_download_directory(
                    str(folders_cfg.get(CONF_MEDIA_LIBRARY_PATH) or download_path)
                )
            except (OSError, ValueError):
                errors["base"] = "cannot_create_folder"

            normalized = self._normalize_options(user_input)
            normalized[CONF_MEDIA_TARGETS] = raw_media_targets(self.config_entry)
            mobile_cfg = normalized[SECTION_NOTIFY_MOBILE]
            zalo_cfg = normalized[SECTION_NOTIFY_ZALO]

            if not errors and mobile_cfg[CONF_NOTIFY_ENABLED]:
                mobile_action = mobile_cfg[CONF_MOBILE_NOTIFY_ACTION]
                if not mobile_action:
                    errors["base"] = "mobile_action_required"
                elif mobile_action not in available_mobile_actions:
                    errors["base"] = "mobile_action_unavailable"

            if not errors and zalo_cfg[CONF_NOTIFY_ENABLED]:
                if not zalo_cfg[CONF_ZALO_THREAD_ID] or not zalo_cfg[CONF_ZALO_ACCOUNT]:
                    errors["base"] = "zalo_fields_required"

            if not errors:
                assert download_path is not None
                assert library_path is not None

                folders_changed = (
                    download_path != current_download
                    or library_path != current_library
                )

                if folders_changed:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            CONF_FILE_PATH: download_path,
                            CONF_MEDIA_LIBRARY_PATH: library_path,
                        },
                    )

                # Notification settings remain in ConfigEntry.options so the
                # existing download manager can read them without any changes.
                result = self.async_create_entry(title="", data=normalized)

                # Folder paths are consumed when the manager/playback manager is
                # constructed. Reload only when a folder actually changed; pure
                # notification edits remain reload-free.
                if folders_changed:
                    await self.hass.config_entries.async_reload(
                        self.config_entry.entry_id
                    )
                return result

        return self.async_show_form(
            step_id="settings",
            data_schema=self._options_schema(user_input, available_mobile_actions),
            errors=errors,
        )

    async def async_step_media_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage which HA media_player entities the integration may expose."""
        targets = raw_media_targets(self.config_entry)
        menu_options = ["add_media_target"]
        if targets:
            menu_options.extend(["edit_media_target", "remove_media_target"])
        return self.async_show_menu(
            step_id="media_targets",
            menu_options=menu_options,
            description_placeholders={
                "configured_count": str(len(targets)),
                "configured_targets": self._target_summary(targets),
            },
        )

    async def async_step_add_media_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one HA media_player to add."""
        errors: dict[str, str] = {}
        targets = raw_media_targets(self.config_entry)
        configured = {item[CONF_TARGET_ENTITY_ID] for item in targets}
        candidates = [
            entity_id
            for entity_id in media_player_candidates(self.hass)
            if entity_id not in configured
        ]
        if user_input is not None:
            entity_id = str(user_input.get(CONF_TARGET_ENTITY_ID) or "")
            if entity_id in configured:
                errors["base"] = "target_already_configured"
            elif self.hass.states.get(entity_id) is None:
                errors["base"] = "target_unavailable"
            else:
                self._pending_target_entity_id = entity_id
                return await self.async_step_media_target_details()

        if not candidates:
            return self.async_abort(reason="no_media_targets_available")
        return self.async_show_form(
            step_id="add_media_target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_ENTITY_ID): EntitySelector(
                        EntitySelectorConfig(
                            domain="media_player",
                            include_entities=candidates,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_media_target_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name and classify a selected playback target."""
        entity_id = self._pending_target_entity_id
        if not entity_id:
            return await self.async_step_media_targets()
        state = self.hass.states.get(entity_id)
        if state is None:
            self._pending_target_entity_id = None
            return await self.async_step_media_targets()

        existing = next(
            (
                item
                for item in raw_media_targets(self.config_entry)
                if item[CONF_TARGET_ENTITY_ID] == entity_id
            ),
            None,
        )
        default_name = (
            existing[CONF_TARGET_NAME]
            if existing and existing[CONF_TARGET_NAME]
            else str(state.attributes.get("friendly_name") or entity_id)
        )
        default_type = (
            existing[CONF_TARGET_TYPE]
            if existing
            else guess_target_type(self.hass, entity_id)
        )
        if user_input is not None:
            name = str(user_input.get(CONF_TARGET_NAME) or default_name).strip() or default_name
            target_type = str(user_input.get(CONF_TARGET_TYPE) or default_type)
            if target_type not in {TARGET_TYPE_SPEAKER, TARGET_TYPE_DLNA, TARGET_TYPE_TV}:
                target_type = default_type
            targets = [
                item
                for item in raw_media_targets(self.config_entry)
                if item[CONF_TARGET_ENTITY_ID] != entity_id
            ]
            targets.append(
                {
                    CONF_TARGET_ENTITY_ID: entity_id,
                    CONF_TARGET_NAME: name,
                    CONF_TARGET_TYPE: target_type,
                }
            )
            self._pending_target_entity_id = None
            return self.async_create_entry(
                title="",
                data={**dict(self.config_entry.options), CONF_MEDIA_TARGETS: targets},
            )

        return self.async_show_form(
            step_id="media_target_details",
            description_placeholders={"entity_id": entity_id},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_NAME, default=default_name): _TEXT_SELECTOR,
                    vol.Required(CONF_TARGET_TYPE, default=default_type): SelectSelector(
                        SelectSelectorConfig(
                            options=[TARGET_TYPE_SPEAKER, TARGET_TYPE_DLNA, TARGET_TYPE_TV],
                            translation_key="target_type",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_edit_media_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose an existing target to rename or reclassify."""
        targets = raw_media_targets(self.config_entry)
        if not targets:
            return await self.async_step_media_targets()
        if user_input is not None:
            self._pending_target_entity_id = str(
                user_input.get(CONF_TARGET_ENTITY_ID) or ""
            )
            return await self.async_step_media_target_details()
        return self.async_show_form(
            step_id="edit_media_target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_ENTITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._target_options(targets),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_remove_media_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one target from the integration-managed picker."""
        targets = raw_media_targets(self.config_entry)
        if not targets:
            return await self.async_step_media_targets()
        if user_input is not None:
            entity_id = str(user_input.get(CONF_TARGET_ENTITY_ID) or "")
            targets = [
                item for item in targets if item[CONF_TARGET_ENTITY_ID] != entity_id
            ]
            return self.async_create_entry(
                title="",
                data={**dict(self.config_entry.options), CONF_MEDIA_TARGETS: targets},
            )
        return self.async_show_form(
            step_id="remove_media_target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_ENTITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._target_options(targets),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _target_options(self, targets: list[dict[str, str]]) -> list[SelectOptionDict]:
        """Build labels for configured targets."""
        type_labels = {
            TARGET_TYPE_SPEAKER: "Loa thường",
            TARGET_TYPE_DLNA: "Loa DLNA",
            TARGET_TYPE_TV: "Tivi",
        }
        return [
            SelectOptionDict(
                value=item[CONF_TARGET_ENTITY_ID],
                label=f"{item[CONF_TARGET_NAME] or item[CONF_TARGET_ENTITY_ID]} · {type_labels[item[CONF_TARGET_TYPE]]}",
            )
            for item in targets
        ]

    def _target_summary(self, targets: list[dict[str, str]]) -> str:
        """Return a compact readable target list for the options menu."""
        if not targets:
            return "Chưa có thiết bị nào"
        return "\n".join(option["label"] for option in self._target_options(targets))

    def _options_schema(
        self,
        user_input: dict[str, Any] | None,
        available_mobile_actions: list[str],
    ) -> vol.Schema:
        """Build the unified form from entry data, options and live notify actions."""
        values = user_input or dict(self.config_entry.options)
        home_cfg = _option_section(values, SECTION_NOTIFY_HOME_ASSISTANT)
        mobile_cfg = _option_section(values, SECTION_NOTIFY_MOBILE)
        zalo_cfg = _option_section(values, SECTION_NOTIFY_ZALO)

        if user_input is not None:
            folders_cfg = _option_section(user_input, SECTION_FOLDERS)
            download_default = str(
                folders_cfg.get(CONF_FILE_PATH)
                or self.config_entry.data[CONF_FILE_PATH]
            )
            library_default = str(
                folders_cfg.get(CONF_MEDIA_LIBRARY_PATH)
                or self.config_entry.data.get(
                    CONF_MEDIA_LIBRARY_PATH, download_default
                )
            )
        else:
            download_default = str(self.config_entry.data[CONF_FILE_PATH])
            library_default = str(
                self.config_entry.data.get(
                    CONF_MEDIA_LIBRARY_PATH, download_default
                )
            )

        selected_mobile = str(mobile_cfg.get(CONF_MOBILE_NOTIFY_ACTION) or "")
        mobile_options = [
            SelectOptionDict(value=action, label=mobile_notify_action_label(action))
            for action in available_mobile_actions
        ]
        if selected_mobile and selected_mobile not in available_mobile_actions:
            mobile_options.append(
                SelectOptionDict(
                    value=selected_mobile,
                    label=selected_mobile,
                )
            )

        mobile_fields: dict[vol.Marker, Any] = {
            vol.Required(
                CONF_NOTIFY_ENABLED,
                default=bool(
                    mobile_cfg.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
                ),
            ): _BOOLEAN_SELECTOR,
        }
        mobile_marker = vol.Optional(CONF_MOBILE_NOTIFY_ACTION)
        if selected_mobile:
            mobile_marker = vol.Optional(
                CONF_MOBILE_NOTIFY_ACTION,
                default=selected_mobile,
            )
        mobile_fields[mobile_marker] = SelectSelector(
            SelectSelectorConfig(
                options=mobile_options,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        return vol.Schema(
            {
                vol.Required(SECTION_FOLDERS): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_FILE_PATH,
                                default=download_default,
                            ): _TEXT_SELECTOR,
                            vol.Required(
                                CONF_MEDIA_LIBRARY_PATH,
                                default=library_default,
                            ): _TEXT_SELECTOR,
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(SECTION_NOTIFY_HOME_ASSISTANT): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_NOTIFY_ENABLED,
                                default=bool(
                                    home_cfg.get(
                                        CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED
                                    )
                                ),
                            ): _BOOLEAN_SELECTOR,
                        }
                    ),
                    {
                        "collapsed": not bool(
                            home_cfg.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
                        )
                    },
                ),
                vol.Required(SECTION_NOTIFY_MOBILE): section(
                    vol.Schema(mobile_fields),
                    {
                        "collapsed": not bool(
                            mobile_cfg.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
                        )
                    },
                ),
                vol.Required(SECTION_NOTIFY_ZALO): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_NOTIFY_ENABLED,
                                default=bool(
                                    zalo_cfg.get(
                                        CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED
                                    )
                                ),
                            ): _BOOLEAN_SELECTOR,
                            vol.Optional(
                                CONF_ZALO_THREAD_ID,
                                default=str(zalo_cfg.get(CONF_ZALO_THREAD_ID) or ""),
                            ): _TEXT_SELECTOR,
                            vol.Optional(
                                CONF_ZALO_ACCOUNT,
                                default=str(zalo_cfg.get(CONF_ZALO_ACCOUNT) or ""),
                            ): _TEXT_SELECTOR,
                            vol.Required(
                                CONF_ZALO_TYPE,
                                default=str(
                                    zalo_cfg.get(CONF_ZALO_TYPE) or ZALO_TYPE_USER
                                ),
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=list(ZALO_TYPES),
                                    mode=SelectSelectorMode.LIST,
                                    translation_key="zalo_type",
                                )
                            ),
                        }
                    ),
                    {
                        "collapsed": not bool(
                            zalo_cfg.get(CONF_NOTIFY_ENABLED, DEFAULT_NOTIFY_ENABLED)
                        )
                    },
                ),
            }
        )

    @staticmethod
    def _normalize_options(user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize notification data while folder paths stay in ConfigEntry.data."""
        home_cfg = _option_section(user_input, SECTION_NOTIFY_HOME_ASSISTANT)
        mobile_cfg = _option_section(user_input, SECTION_NOTIFY_MOBILE)
        zalo_cfg = _option_section(user_input, SECTION_NOTIFY_ZALO)
        zalo_type = str(zalo_cfg.get(CONF_ZALO_TYPE) or ZALO_TYPE_USER)
        if zalo_type not in ZALO_TYPES:
            zalo_type = ZALO_TYPE_USER

        return {
            SECTION_NOTIFY_HOME_ASSISTANT: {
                CONF_NOTIFY_ENABLED: bool(home_cfg.get(CONF_NOTIFY_ENABLED, False)),
            },
            SECTION_NOTIFY_MOBILE: {
                CONF_NOTIFY_ENABLED: bool(mobile_cfg.get(CONF_NOTIFY_ENABLED, False)),
                CONF_MOBILE_NOTIFY_ACTION: str(
                    mobile_cfg.get(CONF_MOBILE_NOTIFY_ACTION) or ""
                ).strip(),
            },
            SECTION_NOTIFY_ZALO: {
                CONF_NOTIFY_ENABLED: bool(zalo_cfg.get(CONF_NOTIFY_ENABLED, False)),
                CONF_ZALO_THREAD_ID: str(
                    zalo_cfg.get(CONF_ZALO_THREAD_ID) or ""
                ).strip(),
                CONF_ZALO_ACCOUNT: str(zalo_cfg.get(CONF_ZALO_ACCOUNT) or "").strip(),
                CONF_ZALO_TYPE: zalo_type,
            },
        }


def _option_section(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return one nested section as a mutable dictionary."""
    value = data.get(key)
    return dict(value) if isinstance(value, dict) else {}
