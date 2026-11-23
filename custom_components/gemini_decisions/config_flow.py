"""Config flow for Gemini Decisions."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

import probatio

from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import DecisionsAuthError, DecisionsClient, DecisionsError, DecisionsPermissionError
from .const import (
    CONF_ALLOW_ATTACHMENTS,
    CONF_KEY_SOURCE,
    CONF_MAX_ATTACHMENT_MB,
    CONF_MAX_PER_DAY,
    CONF_MAX_PER_MINUTE,
    CONF_MODEL,
    CONF_RESOLUTION,
    CONF_SOURCE_ENTRY_ID,
    CONF_TIMEOUT,
    DEFAULT_ALLOW_ATTACHMENTS,
    DEFAULT_MAX_ATTACHMENT_MB,
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MAX_PER_MINUTE,
    DEFAULT_MODEL,
    DEFAULT_RESOLUTION,
    DEFAULT_TIMEOUT,
    DEFAULT_TITLE,
    DOMAIN,
    GOOGLE_GEMINI_DOMAIN,
    KEY_SOURCE_MANUAL,
    LOGGER,
    RESOLUTIONS,
)

MODEL_PATTERN = re.compile(r"(models/)?[A-Za-z0-9._-]{1,64}")

API_KEY_SCHEMA = probatio.Schema(
    {
        probatio.Required(probatio.Secret(CONF_API_KEY)): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _gemini_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Google Gemini entries that hold a usable key."""
    return [
        entry
        for entry in hass.config_entries.async_entries(GOOGLE_GEMINI_DOMAIN)
        if entry.source != SOURCE_IGNORE
        and entry.disabled_by is None
        and isinstance(entry.data.get(CONF_API_KEY), str)
        and entry.data[CONF_API_KEY]
    ]


async def _validate(hass: HomeAssistant, api_key: str) -> str | None:
    """Return an error key, or None when the key works."""
    client = DecisionsClient(async_get_clientsession(hass), api_key)
    try:
        await client.async_validate_key()
    except DecisionsAuthError:
        return "invalid_auth"
    except DecisionsPermissionError:
        return "permission_denied"
    except DecisionsError:
        return "cannot_connect"
    except Exception:
        LOGGER.exception("Unexpected error checking the key")
        return "unknown"
    return None


class GeminiDecisionsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Gemini Decisions."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the key comes from."""
        entries = _gemini_entries(self.hass)
        if not entries:
            return await self.async_step_api_key()

        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input[CONF_KEY_SOURCE]
            if source == KEY_SOURCE_MANUAL:
                return await self.async_step_api_key()
            chosen = next((e for e in entries if e.entry_id == source), None)
            if chosen is None:
                errors["base"] = "unknown"
            else:
                api_key = chosen.data[CONF_API_KEY]
                if (error := await _validate(self.hass, api_key)) is None:
                    return self.async_create_entry(
                        title=DEFAULT_TITLE,
                        data={CONF_API_KEY: api_key, CONF_SOURCE_ENTRY_ID: chosen.entry_id},
                    )
                errors["base"] = error

        options = [
            SelectOptionDict(value=e.entry_id, label=f"Use the key from {e.title or 'Google Gemini'}")
            for e in entries
        ]
        options.append(SelectOptionDict(value=KEY_SOURCE_MANUAL, label="Enter a different key"))
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_KEY_SOURCE, default=entries[0].entry_id): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste a key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if (error := await _validate(self.hass, api_key)) is None:
                return self.async_create_entry(
                    title=DEFAULT_TITLE, data={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="api_key", data_schema=API_KEY_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Google rejected the stored key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if (error := await _validate(self.hass, api_key)) is None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=API_KEY_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if (error := await _validate(self.hass, api_key)) is None:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure", data_schema=API_KEY_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return GeminiDecisionsOptionsFlow()


def _number(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    )


OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_MAX_PER_MINUTE, default=DEFAULT_MAX_PER_MINUTE): _number(1, 200),
        probatio.Required(CONF_MAX_PER_DAY, default=DEFAULT_MAX_PER_DAY): _number(1, 10000),
        probatio.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): _number(5, 120),
        probatio.Required(CONF_ALLOW_ATTACHMENTS, default=DEFAULT_ALLOW_ATTACHMENTS): BooleanSelector(),
        probatio.Required(CONF_RESOLUTION, default=DEFAULT_RESOLUTION): SelectSelector(
            SelectSelectorConfig(options=list(RESOLUTIONS), mode=SelectSelectorMode.DROPDOWN)
        ),
        probatio.Required(CONF_MAX_ATTACHMENT_MB, default=DEFAULT_MAX_ATTACHMENT_MB): _number(1, 40),
        probatio.Required(CONF_MODEL, default=DEFAULT_MODEL): TextSelector(),
    }
)


class GeminiDecisionsOptionsFlow(OptionsFlow):
    """Change limits, attachments and the model."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            model = str(user_input[CONF_MODEL]).strip()
            if MODEL_PATTERN.fullmatch(model) is None:
                errors[CONF_MODEL] = "invalid_model"
            else:
                return self.async_create_entry(data={**user_input, CONF_MODEL: model})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input or self.config_entry.options
            ),
            errors=errors,
        )
