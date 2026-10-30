"""Model cards refresh by themselves (TD-5197).

A little after the daemon starts, and then every ``model_refresh.every_hours``,
each preset's tiers are refreshed from their hosts' lists the way Settings'
Refresh does, off the chat path. A slug typed with other capitals takes the
host's card and spelling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tests.test_model_cards import CARD, KEY, NO_CACHE_PRICE, OPENROUTER, Hosts
from tstd import daemon as daemon_module
from tstd import model_cards
from tstd.card_write import save_tier_values
from tstd.config import TierConfig, load_config
from tstd.mock import MockProvider, Script
from tstd.model_setup import list_endpoint_models
from tstd.protocol import EndpointModels


@pytest.fixture
def card_host(monkeypatch: pytest.MonkeyPatch) -> Hosts:
    """OpenRouter's list behind a MockTransport; the keychain returns KEY."""
    hosts = Hosts({OPENROUTER: {"data": [CARD, NO_CACHE_PRICE]}})

    async def listing(base_url: str, api_key: str | None, **_: Any) -> EndpointModels:
        async with hosts.client() as client:
            return await list_endpoint_models(base_url, api_key, client=client)

    async def read(name: str = "openrouter") -> str:
        return KEY

    monkeypatch.setattr(model_cards, "list_endpoint_models", listing)
    monkeypatch.setattr(daemon_module, "get_api_key", read)
    return hosts


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first pass runs at once instead of 30 s after start."""
    monkeypatch.setattr(daemon_module, "CARD_REFRESH_FIRST_DELAY", 0.05)


def _data_dir(tmp_path: Path, *, auto: bool = True) -> Path:
    """The test catalog, its brain saved as ``MoonshotAI/Kimi-K3`` at 9/9."""
    path = tmp_path / "config.yaml"
    text = catalog_config_yaml()
    shipped = "model_refresh:\n  auto: true\n"
    assert shipped in text
    if not auto:
        text = text.replace(shipped, "model_refresh:\n  auto: false\n", 1)
    path.write_text(text, encoding="utf-8")
    save_tier_values(
        "tst-default",
        "brain",
        {"slug": "MoonshotAI/Kimi-K3", "input_price": 9.0, "output_price": 9.0},
        path=path,
    )
    return tmp_path


def _brain(data_dir: Path) -> TierConfig:
    return load_config(data_dir / "config.yaml").presets["tst-default"].brain


async def _until(check: Callable[[], bool], seconds: float = 5.0) -> bool:
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


def _asked(hosts: Hosts) -> int:
    return sum(1 for url, _auth in hosts.requests if url == f"{OPENROUTER}/models")


async def test_a_background_pass_fixes_the_spelling_and_the_prices(
    tmp_path: Path, card_host: Hosts
) -> None:
    data_dir = _data_dir(tmp_path)
    assert _brain(data_dir).slug == "MoonshotAI/Kimi-K3"
    _daemon, task, ws = await _start(data_dir, MockProvider(default=Script(kind="text")))
    try:
        assert await _until(lambda: _brain(data_dir).slug == CARD["id"])
        brain = _brain(data_dir)
        assert (brain.input_price, brain.output_price) == (2.0, 10.0)
        state = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
        card = state["tier_cards"]["brain"]
        assert card["card"] == "ok"
        assert card["detail"].startswith("Updated automatically:")
        assert "model name" in card["detail"] and "input price" in card["detail"]
    finally:
        await _stop(task, ws)


async def test_auto_off_leaves_refreshing_to_settings(tmp_path: Path, card_host: Hosts) -> None:
    data_dir = _data_dir(tmp_path, auto=False)
    _daemon, task, ws = await _start(data_dir, MockProvider(default=Script(kind="text")))
    try:
        await asyncio.sleep(0.5)
        assert _asked(card_host) == 0
        assert _brain(data_dir).slug == "MoonshotAI/Kimi-K3"
    finally:
        await _stop(task, ws)


async def test_a_down_host_leaves_config_alone(
    tmp_path: Path, card_host: Hosts, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    card_host.fail.add(OPENROUTER)
    data_dir = _data_dir(tmp_path)
    before = (data_dir / "config.yaml").read_text(encoding="utf-8")
    _daemon, task, ws = await _start(data_dir, MockProvider(default=Script(kind="text")))
    try:
        assert await _until(lambda: _asked(card_host) >= 1)
        assert await _until(
            lambda: any(
                getattr(r, "extra_fields", {}).get("status") == "fetch_failed"
                and getattr(r, "extra_fields", {}).get("auto") is True
                for r in caplog.records
            )
        )
        # The daemon still answers, and nothing was written.
        state = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
        assert state["type"] == "setup_state"
        assert (data_dir / "config.yaml").read_text(encoding="utf-8") == before
    finally:
        await _stop(task, ws)


async def test_the_pass_repeats_every_period(
    tmp_path: Path, card_host: Hosts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # every_hours is at least 1; shrink the hour so the second pass comes soon.
    monkeypatch.setattr(daemon_module, "CARD_REFRESH_HOUR", 0.1)
    data_dir = _data_dir(tmp_path)
    _daemon, task, ws = await _start(data_dir, MockProvider(default=Script(kind="text")))
    try:
        assert await _until(lambda: _asked(card_host) >= 3)
    finally:
        await _stop(task, ws)


async def test_a_turn_does_not_wait_for_a_slow_refresh(
    tmp_path: Path, card_host: Hosts, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(base_url: str, api_key: str | None, **_: Any) -> EndpointModels:
        started.set()
        await release.wait()
        return EndpointModels(base_url=base_url, error="still slow")

    monkeypatch.setattr(model_cards, "list_endpoint_models", slow)
    data_dir = _data_dir(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _daemon, task, ws = await _start(
        data_dir, MockProvider(default=Script(kind="stream", content="hi"))
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        opened = await _ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
        sid = opened["session_id"]
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "hi"}))
        done = None
        for _ in range(200):
            event = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if event.get("type") == "turn_complete" and event.get("session_id") == sid:
                done = event
                break
        assert done is not None
        assert done.get("failed") is not True, done.get("error_code")
        # The refresh was still waiting on its host the whole time.
        assert not release.is_set()
    finally:
        release.set()
        await _stop(task, ws)
