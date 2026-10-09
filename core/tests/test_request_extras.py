"""Tier request extras (TD-5008).

``max_output_tokens`` is sent as ``max_tokens`` on the turn, the progress
check and the delegate worker. A tier's ``extra_body`` rides on those and
on the classifier, keys the request owns are a load error, and the
settings writers leave the block alone.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_cap_enforcement import make_echo_session, wait_for_state
from tests.test_delegate import _parent_dispatcher, _workspace, _zero_arg_factory
from tests.test_dispatch import start_loop as start_dispatch_loop
from tests.test_loop import start_loop, wait_for_turn
from tstd.autonomy.runner import DOD_MET, first_prompt
from tstd.boundary_config import BoundaryConfig
from tstd.config import (
    EXTRA_BODY_RESERVED_KEYS,
    ConfigError,
    ModelConfig,
    Preset,
    TierConfig,
    load_config,
)
from tstd.config_write import save_active_preset, save_tier_credential, save_tier_slug
from tstd.context import PromptAssembler
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.progress_guard import PROGRESS_CHECK_INSTRUCTION, TOOL_WINDOW
from tstd.provider import ChatCompletionRequest, content_as_text
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools.delegate import DelegateRuntime, run_worker_child

_THINK_LOW: dict[str, Any] = {"chat_template_kwargs": {"reasoning_effort": "low"}}
_WORKER_EXTRA: dict[str, Any] = {**_THINK_LOW, "top_k": 20}
_BRAIN_MAX = 1_234
_WORKER_MAX = 777


def _tier(slug: str, max_output_tokens: int, extra_body: dict[str, Any] | None) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://mock.local/v1",
        input_price=1.0,
        output_price=2.0,
        cache_read_price=0.5,
        context_window=100_000,
        max_output_tokens=max_output_tokens,
        extra_body=extra_body,
    )


def _config() -> ModelConfig:
    return ModelConfig(
        presets={
            "test": Preset(
                brain=_tier("test-brain", _BRAIN_MAX, _THINK_LOW),
                worker=_tier("test-worker", _WORKER_MAX, _WORKER_EXTRA),
                validator=_tier("test-validator", 999, None),
            ),
        },
        active_preset="test",
    )


def _tier_yaml(extra: str) -> str:
    return (
        "presets:\n"
        "  mine:\n"
        "    brain: &tier\n"
        "      slug: test-brain\n"
        "      base_url: http://mock.local/v1\n"
        "      input_price: 0.0\n"
        "      output_price: 0.0\n"
        "      cache_read_price: 0.0\n"
        "      context_window: 32768\n"
        "      max_output_tokens: 4096\n"
        f"{extra}"
        "    worker: *tier\n"
        "    validator: *tier\n"
        "active_preset: mine\n"
    )


def _load(tmp_path: Path, text: str) -> ModelConfig:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


# ── Config ──────────────────────────────────────────────────────────────


def test_extra_body_is_optional() -> None:
    tier = _tier("m", 100, None)
    assert tier.extra_body is None
    body = ChatCompletionRequest(model="m", messages=[], extra_body=tier.extra_body).to_dict()
    assert set(body) == {"model", "messages"}


def test_extra_body_loads_nested_values(tmp_path: Path) -> None:
    extra = (
        "      extra_body:\n"
        "        chat_template_kwargs:\n"
        "          reasoning_effort: low\n"
        "        top_k: 20\n"
    )
    config = _load(tmp_path, _tier_yaml(extra))
    assert config.tier("brain").extra_body == {
        "chat_template_kwargs": {"reasoning_effort": "low"},
        "top_k": 20,
    }


@pytest.mark.parametrize("key", sorted(EXTRA_BODY_RESERVED_KEYS))
def test_reserved_keys_are_a_load_error(tmp_path: Path, key: str) -> None:
    extra = f"      extra_body:\n        {key}: 1\n"
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tier_yaml(extra))
    message = str(caught.value)
    assert "presets.mine.brain.extra_body" in message
    assert f"extra_body cannot set {key}:" in message
    assert "TST Desk sends it itself" in message


def test_every_clash_is_named_once(tmp_path: Path) -> None:
    extra = "      extra_body: {stream: false, model: other, top_p: 0.9}\n"
    with pytest.raises(
        ConfigError, match="extra_body cannot set model, stream: TST Desk sends them"
    ):
        _load(tmp_path, _tier_yaml(extra))


def test_reserved_set_is_what_the_request_owns() -> None:
    assert {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
    } == EXTRA_BODY_RESERVED_KEYS


def test_extra_body_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="extra_body"):
        _load(tmp_path, _tier_yaml("      extra_body: low\n"))


_ROUND_TRIP = """\
# My own preset. The comments stay.
presets:
  mine:
    brain:
      # Keep EZER's thinking short so the answer fits.
      extra_body:
        chat_template_kwargs:
          reasoning_effort: low
      base_url: http://127.0.0.1:8000/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
    worker:
      slug: test-worker
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
      extra_body: {top_k: 20, chat_template_kwargs: {reasoning_effort: low}}
    validator:
      slug: test-validator
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
  other:
    brain:
      slug: other-brain
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
      extra_body:
        top_k: 40
    worker:
      slug: other-worker
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
    validator:
      slug: other-validator
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
active_preset: other
"""


def test_settings_writes_keep_extra_body(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_ROUND_TRIP, encoding="utf-8")
    before = load_config(path)

    save_tier_slug("mine", "brain", "local-brain", path)
    save_tier_slug("mine", "worker", "new-worker", path)
    save_tier_credential("mine", "worker", "openrouter", path)
    save_tier_credential("other", "brain", "openrouter", path)
    save_tier_credential("other", "brain", None, path)
    save_active_preset("mine", path)
    after = load_config(path)

    for preset in ("mine", "other"):
        for tier in ("brain", "worker", "validator"):
            was = getattr(before.presets[preset], tier).extra_body
            now = getattr(after.presets[preset], tier).extra_body
            assert now == was, f"{preset}.{tier}"
    assert after.presets["mine"].brain.extra_body == _THINK_LOW
    assert after.presets["mine"].worker.extra_body == _WORKER_EXTRA
    assert after.presets["other"].brain.extra_body == {"top_k": 40}
    assert after.presets["mine"].brain.slug == "local-brain"
    assert after.presets["mine"].worker.slug == "new-worker"
    assert after.presets["mine"].worker.credential == "openrouter"
    assert after.active_preset == "mine"
    text = path.read_text(encoding="utf-8")
    assert "# Keep EZER's thinking short so the answer fits." in text
    assert "      extra_body: {top_k: 20, chat_template_kwargs: {reasoning_effort: low}}" in text


# ── Requests ────────────────────────────────────────────────────────────


def _assert_tier_extras(body: dict[str, Any], max_tokens: int, extra: dict[str, Any]) -> None:
    assert body["max_tokens"] == max_tokens
    for key, value in extra.items():
        assert body[key] == value


async def test_main_turn_sends_max_tokens_and_extra_body(tmp_path: Path) -> None:
    mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="hi")})
    session = Session(str(tmp_path))
    runner = await start_loop(session, TierRouter(), mock, _config())
    try:
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert [call.model for call in mock.calls] == ["test-brain"]
    body = mock.calls[0].to_dict()
    _assert_tier_extras(body, _BRAIN_MAX, _THINK_LOW)
    assert body["model"] == "test-brain"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][-1] == {"role": "user", "content": "hello"}


async def test_progress_check_sends_them_too(tmp_path: Path) -> None:
    session, dispatcher = make_echo_session(tmp_path, BoundaryConfig())
    calls = [
        Script(kind="tool_call", tool_name="echo", tool_arguments=json.dumps({"message": f"r{i}"}))
        for i in range(TOOL_WINDOW)
    ]
    calls.append(Script(kind="stream", content="DONE: all read"))
    mock = MockProvider(sequences={"test-brain": calls})
    runner = await start_dispatch_loop(
        session, TierRouter(lead_turns=80), mock, _config(), None, dispatcher
    )
    try:
        await session.add_user_message("read everything")
        await wait_for_turn(session, 1, 8.0)
    finally:
        await runner.cancel()

    checks = [
        call
        for call in mock.calls
        if call.messages
        and content_as_text(call.messages[-1].content) == PROGRESS_CHECK_INSTRUCTION
    ]
    assert len(checks) == 1
    body = checks[0].to_dict()
    _assert_tier_extras(body, _BRAIN_MAX, _THINK_LOW)
    assert "tools" not in body
    turn_calls = [call for call in mock.calls if call.model == "test-brain" and call not in checks]
    assert all(call.max_tokens == _BRAIN_MAX for call in turn_calls)


async def test_delegate_worker_sends_the_worker_tier(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "notes.md"}'
                ),
                Script(kind="text", content="notes.md says hello from notes."),
            ]
        }
    )
    config = _config()
    parent.cost_tracker = CostTracker(config)
    parent.delegate_runtime = DelegateRuntime(
        provider_factory=_zero_arg_factory(mock),
        config=config,
        dispatcher=dispatcher,
        assembler=PromptAssembler(parent.workspace_path),
        session_start=time.time(),
        iterations=0,
    )

    summary = await run_worker_child(parent=parent, task="Read notes.md and summarize")

    assert "hello from notes" in summary
    worker_calls = [call for call in mock.calls if call.model == "test-worker"]
    assert len(worker_calls) == 2
    for call in worker_calls:
        body = call.to_dict()
        _assert_tier_extras(body, _WORKER_MAX, _WORKER_EXTRA)
        assert body["tool_choice"] == "auto"
        assert "stream" not in body


async def test_classifier_keeps_its_cap_and_takes_extra_body(tmp_path: Path) -> None:
    """``_worker_completion`` serves the classifier and the DoD poll."""
    session = Session(str(tmp_path))
    charter = make_charter(max_iterations=8)
    _wire_autonomy(session, charter)
    mock = MockProvider(
        scripts={
            "test-brain": Script(kind="stream", content="Working"),
            "test-worker": Script(kind="text", content="GREEN"),
        }
    )
    runner = await start_loop(session, TierRouter(), mock, _config())
    await session.add_user_message(first_prompt(charter))
    await wait_for_state(session, "complete")
    assert session.autonomy_stop_reason == DOD_MET
    assert not runner.is_running

    worker_calls = [call for call in mock.calls if call.model == "test-worker"]
    assert worker_calls
    for call in worker_calls:
        body = call.to_dict()
        _assert_tier_extras(body, 8, _WORKER_EXTRA)
        assert body["temperature"] == 0.0
