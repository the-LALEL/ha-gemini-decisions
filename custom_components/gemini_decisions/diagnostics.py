"""Diagnostics for Gemini Decisions. Never includes the key or request content."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import DecisionsConfigEntry
from .const import CONF_SOURCE_ENTRY_ID


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DecisionsConfigEntry
) -> dict[str, Any]:
    """Return redacted settings and today's usage."""
    runtime = getattr(entry, "runtime_data", None)
    return {
        "data": async_redact_data(dict(entry.data), {CONF_API_KEY, CONF_SOURCE_ENTRY_ID}),
        "options": dict(entry.options),
        "usage_today": asdict(runtime.limiter.usage) if runtime else None,
    }
