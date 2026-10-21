"""Reasoning effort: the model picker's Low..Max and Ultracode."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_set_tier import (
    _connect_and_handshake,
    _open_workspace,
    _start_daemon,
)
from tstd.config import ModelConfig, Preset, SessionConfig, TierConfig
from tstd.cost import CostTracker
from tstd.effort import DEFAULT_EFFORT, EFFORT_LEVELS, with_effort
from tstd.loop import _stream_and_parse
from tstd.protocol import HandshakeError, SetEffort, TierState, parse_client_message
from tstd.provider import ChatCompletionRequest, ChatMessage, Delta, StreamChunk
from tstd.router import TierRouter
from tstd.session import Session
from tstd.session_store import SessionStore


def _tier(
    slug: str, *, reasoning_effort: bool = False, extra: dict[str, Any] | None = None
) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://gateway.test/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=100_000,
        max_output_tokens=1_000,
        reasoning_effort=reasoning_effort,
        extra_body=extra,
    )


class _Capture:
    """A provider that keeps the request body it was sent."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def chat_completion_stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamChunk]:
        self.bodies.append(request.to_dict())

        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(id="c", delta=Delta(content="ok"), finish_reason="stop")

        return gen()


async def _body_for(slug: str, tier_cfg: TierConfig, effort: str | None) -> dict[str, Any]:
    session = Session("/ws")
    session.router = TierRouter()
    if effort is not None:
        session.router.set_effort(effort)
    provider = _Capture()
    await _stream_and_parse(
        provider,  # type: ignore[arg-type]
        slug,
        [ChatMessage(role="user", content="hi")],
        session,
        None,
        CostTracker(ModelConfig(presets={}, active_preset="")),  # type: ignore[arg-type]
        "brain",
        tier_cfg,
        emit_events=False,
    )
    return provider.bodies[0]


def test_levels_are_the_wire_contract() -> None:
    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max", "ultracode")
    assert DEFAULT_EFFORT == "medium"
    assert SessionConfig().default_effort == "medium"


def test_router_defaults_to_medium_and_rejects_junk() -> None:
    router = TierRouter()
    assert router.effort == "medium"
    router.set_effort("ultracode")
    assert router.effort == "ultracode"
    with pytest.raises(ValueError):
        router.set_effort("extra")
    router.reset()
    assert router.effort == "ultracode"  # a reset clears routing, not the picker


def test_effort_goes_only_to_tiers_that_opt_in() -> None:
    assert with_effort(None, "high", tier_supports=False) is None
    base = {"top_k": 20}
    assert with_effort(base, "max", tier_supports=True) == {
        "top_k": 20,
        "reasoning_effort": "max",
    }
    assert base == {"top_k": 20}


@pytest.mark.parametrize("level", EFFORT_LEVELS)
async def test_request_body_carries_each_level_for_an_opted_in_tier(level: str) -> None:
    body = await _body_for("house", _tier("house", reasoning_effort=True), level)
    assert body["reasoning_effort"] == level


async def test_default_request_is_medium_and_ultracode_goes_out_as_is() -> None:
    tier = _tier("house", reasoning_effort=True)
    assert (await _body_for("house", tier, None))["reasoning_effort"] == "medium"
    assert (await _body_for("house", tier, "ultracode"))["reasoning_effort"] == "ultracode"


async def test_a_tier_that_does_not_opt_in_gets_no_effort() -> None:
    assert "reasoning_effort" not in await _body_for("plain", _tier("plain"), "high")


async def test_picked_level_beats_the_tiers_own_extra_body() -> None:
    tier = _tier("house", reasoning_effort=True, extra={"reasoning_effort": "low", "top_k": 5})
    body = await _body_for("house", tier, "xhigh")
    assert body["reasoning_effort"] == "xhigh"
    assert body["top_k"] == 5


def test_preset_picker_fields_default_to_primary() -> None:
    tier = _tier("house")
    plain = Preset(brain=tier, worker=tier, validator=tier)
    assert plain.picker == "primary" and plain.label is None
    more = Preset(brain=tier, worker=tier, validator=tier, picker="more", label="Raw")
    assert more.picker == "more" and more.label == "Raw"


def test_set_effort_message_round_trips_and_checks_the_level() -> None:
    msg = parse_client_message(
        json.dumps({"type": "set_effort", "session_id": "s1", "effort": "xhigh"})
    )
    assert isinstance(msg, SetEffort)
    assert msg.effort == "xhigh"
    with pytest.raises(HandshakeError):
        parse_client_message(
            json.dumps({"type": "set_effort", "session_id": "s1", "effort": "extra"})
        )
    assert TierState(session_id="s", tier="brain", seq=1).effort == "medium"


async def test_effort_persists_on_the_session_row(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    await store.upsert("s1", "/ws", "idle")
    assert next(r for r in store.records() if r.session_id == "s1").effort is None
    assert await store.set_effort("s1", "max")
    assert not await store.set_effort("nope", "max")
    again = SessionStore(tmp_path)
    assert next(r for r in again.records() if r.session_id == "s1").effort == "max"
    await again.upsert("s1", "/ws", "running")  # a state change keeps it
    assert next(r for r in again.records() if r.session_id == "s1").effort == "max"


async def test_set_effort_over_the_wire_acks_with_tier_state(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        daemon, task = await _start_daemon(tmp)
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        state = await _open_workspace(ws, str(tmp_path))
        sid = state["session_id"]
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.router is not None
        assert sess.router.effort == "medium"

        await ws.send(json.dumps({"type": "set_effort", "session_id": sid, "effort": "ultracode"}))
        await asyncio.sleep(0.2)
        assert sess.router.effort == "ultracode"
        tier_states = [e for e in sess.event_log.all_events if isinstance(e, TierState)]
        assert tier_states[-1].effort == "ultracode"
        record = next(r for r in daemon._session_store.records() if r.session_id == sid)
        assert record.effort == "ultracode"

        await ws.close()
        daemon._shutdown_event.set()
        await asyncio.gather(task, return_exceptions=True)
