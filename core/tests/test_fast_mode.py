"""TD-5176: the Fast toggle.

A local model's switch borrows hardware; an API model's Fast is request fields.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_effort import _body_for, _tier
from tests.test_set_tier import _connect_and_handshake, _open_workspace, _start_daemon
from tstd import fast_switch
from tstd.config import FastConfig, ModelConfig, Preset
from tstd.loop import with_fast
from tstd.protocol import SetFast, TierState, parse_client_message
from tstd.router import TierRouter


class _Switch:
    """A loopback fast switch like a GPU lender: POST turns on/off, GET reports."""

    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.on = False
        self.posts: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._send(200, {"forge": {"on": outer.on, "last": "01:46:57 off: idle 20 min"}})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append(body)
                if outer.refuse and body["on"]:
                    self._send(409, {"ok": False, "error": outer.refuse})
                    return
                outer.on = bool(body["on"])
                self._send(200, {"ok": True, "model": "forge", "on": outer.on})

            def log_message(self, *a: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/fast"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def switch() -> Iterator[_Switch]:
    s = _Switch()
    yield s
    s.close()


# ── config ──────────────────────────────────────────────────────────────


def test_fast_needs_a_switch_or_request_fields() -> None:
    with pytest.raises(ValidationError, match="hook_url, extra_body"):
        FastConfig()
    assert FastConfig(extra_body={"service_tier": "priority"}).hook_url is None


def test_the_switch_must_be_on_this_machine() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        FastConfig(hook_url="http://10.0.0.5:8012/fast")
    assert (
        FastConfig(hook_url="http://127.0.0.1:8012/fast", hook_model="forge").hook_model == "forge"
    )


def test_a_preset_without_fast_has_none() -> None:
    assert Preset(brain=_tier("a"), worker=_tier("a"), validator=_tier("a")).fast is None


# ── requests ────────────────────────────────────────────────────────────


def test_with_fast_merges_only_when_on() -> None:
    assert with_fast({"a": 1}, None) == {"a": 1}
    assert with_fast(None, {"service_tier": "priority"}) == {"service_tier": "priority"}
    assert with_fast({"x": {"y": 1}}, {"x": {"z": 2}}) == {"x": {"y": 1, "z": 2}}


def test_router_keeps_fields_only_while_on() -> None:
    router = TierRouter()
    router.set_fast(True, {"service_tier": "priority"})
    assert router.fast and router.fast_extra_body == {"service_tier": "priority"}
    router.set_fast(False, {"service_tier": "priority"})
    assert not router.fast and router.fast_extra_body is None


async def test_an_api_models_fast_fields_ride_on_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = TierRouter.__init__

    def fast_router(self: TierRouter, *a: Any, **k: Any) -> None:
        original(self, *a, **k)
        self.set_fast(True, {"service_tier": "priority"})

    body_off = await _body_for("m", _tier("m"), None)
    monkeypatch.setattr(TierRouter, "__init__", fast_router)
    body_on = await _body_for("m", _tier("m"), None)
    assert "service_tier" not in body_off
    assert body_on["service_tier"] == "priority"


def test_set_fast_message_round_trips() -> None:
    msg = parse_client_message(json.dumps({"type": "set_fast", "session_id": "s", "on": True}))
    assert isinstance(msg, SetFast) and msg.on


# ── the switch ──────────────────────────────────────────────────────────


async def test_switch_on_and_off(switch: _Switch) -> None:
    cfg = FastConfig(hook_url=switch.url, hook_model="forge")
    assert (await fast_switch.switch(cfg, True))["on"] is True
    assert switch.posts[-1] == {"model": "forge", "on": True, "by": "tst-desk"}
    assert await fast_switch.is_on(cfg) == (True, "01:46:57 off: idle 20 min")
    assert (await fast_switch.switch(cfg, False))["on"] is False


async def test_a_refusal_carries_the_switchs_reason() -> None:
    s = _Switch(refuse="chat-model.service is answering someone; try again in a minute")
    try:
        with pytest.raises(fast_switch.FastSwitchError, match="answering someone"):
            await fast_switch.switch(FastConfig(hook_url=s.url, hook_model="forge"), True)
    finally:
        s.close()


async def test_an_unreachable_switch_is_a_clear_error() -> None:
    cfg = FastConfig(hook_url="http://127.0.0.1:9/fast", hook_model="forge")
    with pytest.raises(fast_switch.FastSwitchError, match="did not answer"):
        await fast_switch.switch(cfg, True)
    assert await fast_switch.is_on(cfg) is None


# ── over the wire ───────────────────────────────────────────────────────


def _fast_config(fast: FastConfig | None) -> ModelConfig:
    t = _tier("forge")
    return ModelConfig(
        presets={"forge": Preset(brain=t, worker=t, validator=t, fast=fast)}, active_preset="forge"
    )


async def _wire(
    tmp_path: Path, fast: FastConfig | None, sends: list[dict[str, Any]]
) -> list[TierState]:
    with tempfile.TemporaryDirectory() as tmp:
        daemon, task = await _start_daemon(tmp)
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        sess.config = _fast_config(fast)
        for msg in sends:
            await ws.send(json.dumps({**msg, "session_id": sid}))
            await asyncio.sleep(0.4)
        states = [e for e in sess.event_log.all_events if isinstance(e, TierState)]
        await ws.close()
        daemon._shutdown_event.set()
        await asyncio.gather(task, return_exceptions=True)
        return states


async def test_fast_on_and_off_over_the_wire(tmp_path: Path, switch: _Switch) -> None:
    fast = FastConfig(
        hook_url=switch.url, hook_model="forge", note="Borrows all four GPUs; about 2 min"
    )
    states = await _wire(
        tmp_path, fast, [{"type": "set_fast", "on": True}, {"type": "set_fast", "on": False}]
    )
    on, off = states[-2], states[-1]
    assert on.fast and on.fast_available and on.fast_note == "Borrows all four GPUs; about 2 min"
    assert not off.fast and off.fast_note == "Fast is off"
    assert [p["on"] for p in switch.posts] == [True, False]


async def test_a_refused_switch_leaves_fast_off_and_says_why(tmp_path: Path) -> None:
    s = _Switch(refuse="media is using GPU0 right now; try again when it finishes")
    try:
        states = await _wire(
            tmp_path,
            FastConfig(hook_url=s.url, hook_model="forge"),
            [{"type": "set_fast", "on": True}],
        )
    finally:
        s.close()
    assert not states[-1].fast and "media is using GPU0" in states[-1].fast_note


async def test_an_api_model_turns_on_without_a_switch(tmp_path: Path) -> None:
    states = await _wire(
        tmp_path,
        FastConfig(extra_body={"service_tier": "priority"}),
        [{"type": "set_fast", "on": True}],
    )
    assert states[-1].fast and states[-1].fast_available and states[-1].fast_note == ""


async def test_a_model_without_fast_refuses(tmp_path: Path) -> None:
    states = await _wire(tmp_path, None, [{"type": "set_fast", "on": True}])
    assert all(not s.fast for s in states)


async def test_the_toggle_follows_a_switch_that_ended_fast_on_its_own(
    tmp_path: Path, switch: _Switch
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        daemon, task = await _start_daemon(tmp)
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.router is not None
        sess.config = _fast_config(FastConfig(hook_url=switch.url, hook_model="forge"))
        await ws.send(json.dumps({"type": "set_fast", "session_id": sid, "on": True}))
        await asyncio.sleep(0.4)
        assert sess.router.fast
        switch.on = False  # idle revert at the switch
        await daemon._sync_fast(sess)
        last = [e for e in sess.event_log.all_events if isinstance(e, TierState)][-1]
        assert (
            not sess.router.fast and not last.fast and last.fast_note == "Fast ended (idle 20 min)"
        )
        await ws.close()
        daemon._shutdown_event.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_turns_own_tier_state_keeps_effort_and_fast(
    tmp_path: Path, switch: _Switch
) -> None:
    from tests.test_dispatch import attach_auto_approver, wait_for_turn
    from tests.test_turn_stream_errors import _answer, _Gateway
    from tstd.loop import agent_loop
    from tstd.provider import ProviderClient
    from tstd.session import Session, SessionRunner
    from tstd.tools import ToolDispatcher, ToolRegistry

    session = Session(str(tmp_path))
    gateway = _Gateway(lambda attempt: _answer("ok"))
    client = gateway.client(retries=0)
    config = _fast_config(FastConfig(hook_url=switch.url, hook_model="forge"))
    router = TierRouter()
    router.set_effort("high")
    router.set_fast(True)
    session.fast_note = "Borrows all four GPUs"

    async def factory(*_args: object) -> ProviderClient:
        return client

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            router,
            factory,
            config,
            tool_dispatcher=attach_auto_approver(ToolDispatcher(ToolRegistry())),
        ),
    )
    await runner.start()
    try:
        await session.add_user_message("hi")
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
        await client.close()
    states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
    assert states, "the loop reports its tier at turn start"
    last = states[-1]
    assert (last.effort, last.fast, last.fast_available, last.fast_note) == (
        "high",
        True,
        True,
        "Borrows all four GPUs",
    )
