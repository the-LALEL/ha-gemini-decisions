"""Shared fixtures: a real Home Assistant core plus a fake Google endpoint."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness import fake_google, running_hass  # noqa: E402

ENTITY = "ai_task.gemini_decisions"


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    """Booted core with the fake Google server wired in (no entry yet)."""
    async with fake_google() as (fake, base):
        async with running_hass(tmp_path) as hass:
            from custom_components.gemini_decisions import const

            monkeypatch.setattr(const, "BASE_URL", base)
            yield hass, fake


async def add_entry(hass, options: dict[str, Any] | None = None):
    """Run the config flow with a pasted key and optionally set options."""
    result = await hass.config_entries.flow.async_init(
        "gemini_decisions", context={"source": "user"}
    )
    if result["step_id"] == "user":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"key_source": "manual"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "test-key"}
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("gemini_decisions")[0]
    if options:
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {
            "max_requests_per_minute": 12,
            "max_requests_per_day": 300,
            "timeout": 30,
            "allow_attachments": False,
            "image_resolution": "medium",
            "max_attachment_mb": 20,
            "model": "gemini-decision-1",
        }
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {**defaults, **options}
        )
        assert result["type"] == "create_entry", result
        await hass.async_block_till_done()
    from homeassistant.components.ai_task.const import DATA_PREFERENCES

    hass.data[DATA_PREFERENCES].async_set_preferences(evaluate_entity_id=ENTITY)
    return entry


async def evaluate(hass, questions, state: Any = None, attachments=None):
    data: dict[str, Any] = {"task_name": "test", "questions": questions}
    if state is not None:
        data["state"] = state
    if attachments is not None:
        data["attachments"] = attachments
    return await hass.services.async_call(
        "ai_task", "evaluate", data, blocking=True, return_response=True
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
