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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_MOBILE_NOTIFY_ACTION,
    CONF_NOTIFY_ENABLED,
    CONF_ZALO_ACCOUNT,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    DEFAULT_NOTIFY_ENABLED,
    DOMAIN,
    SECTION_NOTIFY_HOME_ASSISTANT,
    SECTION_NOTIFY_MOBILE,
    SECTION_NOTIFY_ZALO,
    ZALO_TYPE_USER,
    ZALO_TYPES,
)
from .helpers import ensure_writable_directory
from .notifications import mobile_notify_action_label, mobile_notify_actions

_BOOLEAN_SELECTOR = BooleanSelector()
_TEXT_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle YouTube-DLP configuration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the notification options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the media download folder."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                path = await self.hass.async_add_executor_job(
                    ensure_writable_directory, user_input[CONF_FILE_PATH]
                )
            except (OSError, ValueError):
                errors["base"] = "cannot_create_folder"
            else:
                await self.async_set_unique_id(f"{DOMAIN}.downloader")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="YouTube-DLP",
                    data={CONF_FILE_PATH: path},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_FILE_PATH): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the media download folder."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                path = await self.hass.async_add_executor_job(
                    ensure_writable_directory, user_input[CONF_FILE_PATH]
                )
            except (OSError, ValueError):
                errors["base"] = "cannot_create_folder"
            else:
                await self.async_set_unique_id(f"{DOMAIN}.downloader")
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_FILE_PATH: path},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FILE_PATH,
                        default=entry.data[CONF_FILE_PATH],
                    ): str
                }
            ),
            errors=errors,
        )


class OptionsFlowHandler(OptionsFlow):
    """Configure completion notifications without reloading the integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage notification targets and enable switches."""
        errors: dict[str, str] = {}
        available_mobile_actions = mobile_notify_actions(self.hass)

        if user_input is not None:
            normalized = self._normalize_options(user_input)
            mobile_cfg = normalized[SECTION_NOTIFY_MOBILE]
            zalo_cfg = normalized[SECTION_NOTIFY_ZALO]

            if mobile_cfg[CONF_NOTIFY_ENABLED]:
                mobile_action = mobile_cfg[CONF_MOBILE_NOTIFY_ACTION]
                if not mobile_action:
                    errors["base"] = "mobile_action_required"
                elif mobile_action not in available_mobile_actions:
                    errors["base"] = "mobile_action_unavailable"

            if not errors and zalo_cfg[CONF_NOTIFY_ENABLED]:
                if not zalo_cfg[CONF_ZALO_THREAD_ID] or not zalo_cfg[CONF_ZALO_ACCOUNT]:
                    errors["base"] = "zalo_fields_required"

            if not errors:
                # The running manager reads entry.options when a download completes,
                # so saving notification settings does not need an integration reload.
                return self.async_create_entry(title="", data=normalized)

            user_input = normalized

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(user_input, available_mobile_actions),
            errors=errors,
        )

    def _options_schema(
        self,
        user_input: dict[str, Any] | None,
        available_mobile_actions: list[str],
    ) -> vol.Schema:
        """Build the form from current options and live mobile notify actions."""
        values = user_input or dict(self.config_entry.options)
        home_cfg = _option_section(values, SECTION_NOTIFY_HOME_ASSISTANT)
        mobile_cfg = _option_section(values, SECTION_NOTIFY_MOBILE)
        zalo_cfg = _option_section(values, SECTION_NOTIFY_ZALO)

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
        """Normalize nested form data while preserving IDs as exact strings."""
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
