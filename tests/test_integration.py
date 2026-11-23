"""End-to-end tests against the installed Home Assistant core and a fake Google."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from tests.conftest import ENTITY, add_entry, evaluate
from tests.harness import default_answers

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def media(hass, name: str, data: bytes) -> str:
    Path(hass.config.config_dir, "media", name).write_bytes(data)
    return f"media-source://media_source/local/{name}"


# --- setup and config flow -------------------------------------------------


async def test_entity_appears_with_evaluate(env):
    hass, fake = env
    await add_entry(hass)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes["supported_features"] == 8 | 2
    assert fake.headers[0]["x-goog-api-key"] == "test-key"
    assert "key=" not in str(fake.headers)


async def test_bad_key_shows_invalid_auth(env):
    hass, fake = env
    fake.models_status = 400
    result = await hass.config_entries.flow.async_init(
        "gemini_decisions", context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "nope"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_only_one_entry(env):
    hass, _ = env
    await add_entry(hass)
    result = await hass.config_entries.flow.async_init(
        "gemini_decisions", context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] in ("single_instance_allowed", "already_configured")


async def test_reuses_google_gemini_key(env):
    hass, fake = env
    google = ConfigEntry(
        data={"api_key": "gemini-key"},
        discovery_keys={},
        domain="google_generative_ai_conversation",
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title="Google Gemini",
        unique_id=None,
        version=2,
    )
    hass.config_entries._entries[google.entry_id] = google
    result = await hass.config_entries.flow.async_init(
        "gemini_decisions", context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"key_source": google.entry_id}
    )
    assert result["type"] == "create_entry", result
    assert result["data"]["api_key"] == "gemini-key"
    assert fake.headers[-1]["x-goog-api-key"] == "gemini-key"


async def test_options_reject_bad_model(env):
    hass, _ = env
    entry = await add_entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "max_requests_per_minute": 12,
            "max_requests_per_day": 300,
            "timeout": 30,
            "allow_attachments": False,
            "image_resolution": "medium",
            "max_attachment_mb": 20,
            "model": "bad model/../x",
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"model": "invalid_model"}


async def test_unload_and_reload(env):
    hass, _ = env
    entry = await add_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_diagnostics_hide_key(env):
    hass, _ = env
    entry = await add_entry(hass)
    from custom_components.gemini_decisions.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "test-key" not in str(diag)


# --- mapping -----------------------------------------------------------------


async def test_all_types_and_awkward_ids(env):
    hass, fake = env
    await add_entry(hass)
    questions = {
        "door open?": {"type": "noul", "instructions": "Is the door open?"},
        "q0": {
            "type": "choice",
            "instructions": "Who is home?",
            "criteria": {"Danny": "Danny is home", "  nobody ": "No one is home", "nobody": "Empty"},
        },
        "binary_sensor.motion": {
            "type": "score",
            "instructions": "How busy is it?",
            "criteria": ["quiet", "some", "busy"],
        },
    }

    def handler(body):
        assert list(body["questions"]) == ["q0", "q1", "q2"]
        keys = list(body["questions"]["q1"]["criteria"])
        assert keys == ["Danny", "nobody", "nobody (2)"]
        assert body["questions"]["q1"]["criteria"]["nobody"].startswith('Option "  nobody "')
        return 200, {
            "answers": {
                "q0": {"type": "noul", "noul": 0.2, "confidence": 0.8},
                "q1": {
                    "type": "choice",
                    "choice": "nobody",
                    "probabilities": {"nobody": 0.7, "Danny": 0.3},
                    "confidence": 0.7,
                },
                "q2": {
                    "type": "score",
                    "score": 1.2,
                    "probabilities": {"2": 0.3, "0": 0.1, "1": 0.6},
                    "legend": {},
                    "confidence": 0.6,
                },
                "q9": {"type": "noul", "noul": 0.5, "confidence": 0.5},
            }
        }, {}

    fake.handler = handler
    response = await evaluate(hass, questions, state="the door is shut")
    assert response["answers"]["door open?"] == {"type": "noul", "noul": 0.2}
    assert response["answers"]["q0"] == {
        "type": "choice",
        "choice": "  nobody ",
        "probabilities": {"Danny": 0.3, "  nobody ": 0.7, "nobody": 0.0},
    }
    assert response["answers"]["binary_sensor.motion"] == {
        "type": "score",
        "score": 1.2,
        "probabilities": [0.1, 0.6, 0.3],
    }
    assert fake.requests[-1]["input"] == "the door is shut"


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({}, "no answers"),
        ({"q0": {"type": "choice", "choice": "a"}}, "wrong type"),
        ({"q0": {"type": "noul", "noul": 1.5}}, "out-of-range"),
        ({"q0": {"type": "noul", "noul": True}}, "non-number"),
        ({"q0": {"type": "noul", "noul": float("nan")}}, "out-of-range|isn.t JSON"),
    ],
)
async def test_bad_answers_are_errors(env, answers, message):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (200, {"answers": answers}, {})
    with pytest.raises(HomeAssistantError, match=message):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert hass.states.get(ENTITY).attributes["last_error"]


async def test_missing_answer_is_error(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (
        200,
        {"answers": {"q0": {"type": "noul", "noul": 0.5}}},
        {},
    )
    with pytest.raises(HomeAssistantError, match="didn't answer question b"):
        await evaluate(
            hass,
            {
                "a": {"type": "noul", "instructions": "?"},
                "b": {"type": "noul", "instructions": "?"},
            },
            state="s",
        )


async def test_block_in_200_reply(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (200, {"prompt_feedback": {"block_reason": "safety"}}, {})
    with pytest.raises(HomeAssistantError, match="blocked this input \\(safety\\)"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")


async def test_empty_state_is_rejected_before_calling(env):
    hass, fake = env
    await add_entry(hass)
    with pytest.raises(ServiceValidationError):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="  ")
    assert fake.requests == []


# --- errors, retries, limits ----------------------------------------------


async def test_safety_400(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (400, {"error": {"code": "safety", "message": "no"}}, {})
    with pytest.raises(HomeAssistantError, match="blocked this input \\(safety\\)"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert len(fake.requests) == 1


async def test_revoked_key_starts_reauth(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (
        400,
        {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid.",
                   "details": [{"reason": "API_KEY_INVALID"}]}},
        {},
    )
    with pytest.raises(HomeAssistantError, match="re-enter it"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler("gemini_decisions")
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_retry_once_on_429_with_short_retry_after(env):
    hass, fake = env
    await add_entry(hass)
    calls = []

    def handler(body):
        calls.append(1)
        if len(calls) == 1:
            return 429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "1"}
        return 200, default_answers(body), {}

    fake.handler = handler
    response = await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert response["answers"]["x"]["noul"] == 0.9
    assert len(calls) == 2


async def test_no_retry_on_daily_quota(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (
        429,
        {"error": {"code": 429, "message": "Quota exceeded for requests per day"}},
        {},
    )
    with pytest.raises(HomeAssistantError, match="daily quota"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert len(fake.requests) == 1


async def test_timeout_is_not_retried(env):
    hass, fake = env
    await add_entry(hass, {"timeout": 5})
    fake.delay = 12
    with pytest.raises(HomeAssistantError, match="in time"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert len(fake.requests) == 1


async def test_html_reply_is_error(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (200, "<html>captive portal</html>", {})
    with pytest.raises(HomeAssistantError, match="isn't JSON"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")


async def test_daily_cap_raises_repair(env):
    hass, fake = env
    await add_entry(hass, {"max_requests_per_day": 2})
    q = {"x": {"type": "noul", "instructions": "?"}}
    await evaluate(hass, q, state="s")
    await evaluate(hass, q, state="s")
    with pytest.raises(HomeAssistantError, match="daily cap of 2"):
        await evaluate(hass, q, state="s")
    assert len(fake.requests) == 2
    assert ir.async_get(hass).async_get_issue("gemini_decisions", "daily_cap_reached")
    assert hass.states.get(ENTITY).attributes["requests_today"] == 2
    assert hass.states.get(ENTITY).attributes["input_tokens_today"] == 84


async def test_per_minute_cap(env):
    hass, fake = env
    await add_entry(hass, {"max_requests_per_minute": 1})
    q = {"x": {"type": "noul", "instructions": "?"}}
    await evaluate(hass, q, state="s")
    with pytest.raises(HomeAssistantError, match="per minute"):
        await evaluate(hass, q, state="s")


async def test_concurrency_is_capped(env):
    hass, fake = env
    await add_entry(hass)
    fake.delay = 0.5
    q = {"x": {"type": "noul", "instructions": "?"}}
    start = asyncio.get_running_loop().time()
    await asyncio.gather(*(evaluate(hass, q, state="s") for _ in range(4)))
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.95  # 4 calls, 2 at a time, 0.5 s each


# --- attachments -----------------------------------------------------------


async def test_attachments_off_by_default(env):
    hass, fake = env
    await add_entry(hass)
    media_id = media(hass, "snap.jpg", JPEG)
    with pytest.raises(ServiceValidationError, match="turned off"):
        await evaluate(
            hass,
            {"x": {"type": "noul", "instructions": "?"}},
            attachments=[{"media_content_id": media_id, "media_content_type": "image/jpg"}],
        )
    assert fake.requests == []


async def test_image_attachment_without_state(env):
    hass, fake = env
    await add_entry(hass, {"allow_attachments": True, "image_resolution": "low"})
    media_id = media(hass, "snap.jpg", JPEG)
    await evaluate(
        hass,
        {"x": {"type": "noul", "instructions": "Is a parcel at the door?"}},
        attachments=[{"media_content_id": media_id, "media_content_type": "image/jpg; q=1"}],
    )
    blocks = fake.requests[-1]["input"]
    assert blocks == [
        {
            "type": "image",
            "data": base64.b64encode(JPEG).decode(),
            "mime_type": "image/jpeg",
            "resolution": "low",
        }
    ]


async def test_generic_type_is_sniffed(env):
    hass, fake = env
    await add_entry(hass, {"allow_attachments": True})
    media_id = media(hass, "frame.bin", PNG)
    await evaluate(
        hass,
        {"x": {"type": "noul", "instructions": "?"}},
        state={"camera": "porch"},
        attachments=[
            {"media_content_id": media_id, "media_content_type": "application/octet-stream"}
        ],
    )
    blocks = fake.requests[-1]["input"]
    assert blocks[0] == {"type": "text", "text": '{"camera":"porch"}'}
    assert blocks[1]["mime_type"] == "image/png"


async def test_unsupported_type_rejected(env):
    hass, fake = env
    await add_entry(hass, {"allow_attachments": True})
    media_id = media(hass, "notes.txt", b"hello")
    with pytest.raises(ServiceValidationError, match="can't send text/plain"):
        await evaluate(
            hass,
            {"x": {"type": "noul", "instructions": "?"}},
            attachments=[{"media_content_id": media_id, "media_content_type": "text/plain"}],
        )
    assert fake.requests == []


async def test_oversized_attachment_rejected_before_reading(env):
    hass, fake = env
    await add_entry(hass, {"allow_attachments": True, "max_attachment_mb": 1})
    media_id = media(hass, "big.jpg", JPEG + b"\x00" * (1024 * 1024))
    with pytest.raises(ServiceValidationError, match="too large"):
        await evaluate(
            hass,
            {"x": {"type": "noul", "instructions": "?"}},
            attachments=[{"media_content_id": media_id, "media_content_type": "image/jpeg"}],
        )
    assert fake.requests == []


# --- regressions from the code review --------------------------------------


async def test_diagnostics_when_setup_failed(env):
    hass, fake = env
    entry = await add_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    fake.models_status = 400
    await hass.config_entries.async_setup(entry.entry_id)
    from custom_components.gemini_decisions.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "test-key" not in str(diag)


async def test_reload_keeps_daily_count(env):
    hass, fake = env
    entry = await add_entry(hass, {"max_requests_per_day": 1})
    q = {"x": {"type": "noul", "instructions": "?"}}
    await evaluate(hass, q, state="s")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="daily cap of 1"):
        await evaluate(hass, q, state="s")
    assert len(fake.requests) == 1


async def test_cap_checked_before_reading_attachments(env, monkeypatch):
    hass, fake = env
    await add_entry(hass, {"allow_attachments": True, "max_requests_per_day": 1})
    q = {"x": {"type": "noul", "instructions": "?"}}
    await evaluate(hass, q, state="s")
    from custom_components.gemini_decisions import ai_task as entity_module

    def boom(*args):
        raise AssertionError("attachments were read after the cap was hit")

    monkeypatch.setattr(entity_module, "build_body", boom)
    media_id = media(hass, "snap.jpg", JPEG)
    with pytest.raises(HomeAssistantError, match="daily cap"):
        await evaluate(
            hass, q, attachments=[{"media_content_id": media_id, "media_content_type": "image/jpeg"}]
        )


async def test_long_retry_after_surfaces_rate_limit(env):
    hass, fake = env
    await add_entry(hass)
    fake.handler = lambda body: (
        429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "30"}
    )
    with pytest.raises(HomeAssistantError, match="rate limit"):
        await evaluate(hass, {"x": {"type": "noul", "instructions": "?"}}, state="s")
    assert len(fake.requests) == 1


async def test_key_with_newline_is_invalid_not_unknown(env):
    hass, _ = env
    result = await hass.config_entries.flow.async_init(
        "gemini_decisions", context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "abc\ndef"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
