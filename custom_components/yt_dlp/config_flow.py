"""Config flow for YouTube-DLP."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_FILE_PATH

from .const import DOMAIN
from .helpers import ensure_writable_directory


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle YouTube-DLP configuration."""

    VERSION = 1

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
