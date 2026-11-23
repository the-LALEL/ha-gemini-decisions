"""Gemini Decisions: answers Home Assistant's evaluate tasks with Google's Decisions API."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import (
    DecisionsAuthError,
    DecisionsClient,
    DecisionsError,
    RequestLimiter,
    UsageSnapshot,
)
from .const import DOMAIN, ISSUE_DAILY_CAP

PLATFORMS = (Platform.AI_TASK,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
STORAGE_VERSION = 1


@dataclass
class RuntimeData:
    """Objects that live as long as the config entry."""

    client: DecisionsClient
    limiter: RequestLimiter
    store: Store[dict[str, int | str]]


type DecisionsConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (config entries only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DecisionsConfigEntry) -> bool:
    """Check the key with a free call, then add the AI Task entity."""
    client = DecisionsClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    try:
        await client.async_validate_key()
    except DecisionsAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except DecisionsError as err:
        raise ConfigEntryNotReady(str(err)) from err

    store: Store[dict[str, int | str]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.usage"
    )
    saved = await store.async_load() or {}
    today = dt_util.now().date().isoformat()
    usage = UsageSnapshot(day=today)
    if saved.get("day") == today:
        usage.requests = int(saved.get("requests", 0))
        usage.tokens = int(saved.get("tokens", 0))

    entry.runtime_data = RuntimeData(client, RequestLimiter(usage), store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DecisionsConfigEntry) -> bool:
    """Unload the entity and write the usage counters now, so a reload keeps them."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = entry.runtime_data
        await runtime.store.async_save(asdict(runtime.limiter.usage))
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: DecisionsConfigEntry) -> None:
    """Delete the usage counters and any open Repairs issue."""
    runtime = getattr(entry, "runtime_data", None)
    store = runtime.store if runtime else Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.usage"
    )
    await store.async_remove()
    ir.async_delete_issue(hass, DOMAIN, ISSUE_DAILY_CAP)
