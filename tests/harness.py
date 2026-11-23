"""Boot a real Home Assistant core (the installed version) with a fake Google server.

HA's own pytest helpers aren't shipped in the wheel, and the community test
plugin doesn't track 2026.11 yet, so this sets up just enough of core by hand.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import json
from pathlib import Path
import shutil
import socket
from typing import Any

from aiohttp import web

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    category_registry as cr,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    issue_registry as ir,
    label_registry as lr,
)
from homeassistant.helpers import translation  # noqa: F401
from homeassistant.setup import async_setup_component

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeGoogle:
    """Records requests and replies with whatever the test sets."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.models_status = 200
        self.handler: Callable[[dict[str, Any]], tuple[int, Any, dict[str, str]]] = (
            lambda body: (200, default_answers(body), {})
        )
        self.delay = 0.0

    async def models(self, request: web.Request) -> web.Response:
        self.headers.append(dict(request.headers))
        if self.models_status != 200:
            return web.json_response(
                {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                           "status": "INVALID_ARGUMENT",
                           "details": [{"reason": "API_KEY_INVALID"}]}},
                status=self.models_status,
            )
        return web.json_response({"models": [{"name": "models/gemini-decision-1"}]})

    async def decisions(self, request: web.Request) -> web.Response:
        body = json.loads(await request.read())
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.delay:
            await asyncio.sleep(self.delay)
        status, payload, headers = self.handler(body)
        if isinstance(payload, (bytes, str)):
            return web.Response(status=status, body=payload, headers=headers)
        return web.json_response(payload, status=status, headers=headers)


def default_answers(body: dict[str, Any]) -> dict[str, Any]:
    """Plausible answers for every question in a request."""
    answers: dict[str, Any] = {}
    for key, question in body["questions"].items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.9, "confidence": 0.9}
        elif question["type"] == "choice":
            options = sorted(question["criteria"])
            probs = {o: 0.0 for o in options}
            probs[options[0]] = 1.0
            answers[key] = {"type": "choice", "choice": options[0], "probabilities": probs, "confidence": 1.0}
        else:
            n = len(question["criteria"])
            probs = {str(i): 0.0 for i in range(n)}
            probs[str(n - 1)] = 1.0
            answers[key] = {"type": "score", "score": float(n - 1), "probabilities": probs,
                            "legend": {}, "confidence": 1.0}
    return {"answers": answers, "usage": {"total_input_tokens": 42, "total_output_tokens": 0}}


@contextlib.asynccontextmanager
async def fake_google() -> AsyncIterator[tuple[FakeGoogle, str]]:
    fake = FakeGoogle()
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_get("/v1beta/models", fake.models)
    app.router.add_post("/v1beta/decisions", fake.decisions)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield fake, f"http://127.0.0.1:{port}/v1beta"
    finally:
        await runner.cleanup()


@contextlib.asynccontextmanager
async def running_hass(config_dir: Path) -> AsyncIterator[HomeAssistant]:
    """Start core with ai_task and this custom integration available."""
    shutil.copytree(REPO / "custom_components", config_dir / "custom_components")
    media = config_dir / "media"
    media.mkdir(exist_ok=True)
    hass = HomeAssistant(str(config_dir))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    config = {
        "homeassistant": {
            "time_zone": "America/New_York",
            "name": "Test",
            "media_dirs": {"local": str(media)},
        },
        "http": {"server_port": free_port(), "server_host": ["127.0.0.1"]},
        "ai_task": {},
    }
    assert await bootstrap.async_from_config_dict(config, hass) is not None
    await hass.async_block_till_done()
    try:
        yield hass
    finally:
        await hass.async_stop(force=True)
