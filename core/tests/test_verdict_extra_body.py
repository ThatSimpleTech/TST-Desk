"""The worker's verdict calls take their own extras (TD-5012).

An 8-token verdict ends inside a reasoning model's thinking, so the
classifier fails closed to B and a prose DoD item never goes green.
``verdict_extra_body`` is deep-merged over ``extra_body`` on those calls
only: the turn and the delegate worker keep the tier's own extras. An
empty verdict at the cap logs one warning per endpoint and model.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

import tstd.loop as loop_module
from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_cap_enforcement import make_echo_session, wait_for_state
from tests.test_delegate import _parent_dispatcher, _workspace, _zero_arg_factory
from tests.test_dispatch import start_loop as start_dispatch_loop
from tests.test_loop import start_loop, wait_for_turn
from tests.test_request_extras import _load, _tier_yaml
from tstd.autonomy.runner import DOD_MET, first_prompt
from tstd.boundary_config import BoundaryConfig
from tstd.config import (
    EXTRA_BODY_RESERVED_KEYS,
    ConfigError,
    ModelConfig,
    Preset,
    TierConfig,
    load_config,
    merge_extra_body,
)
from tstd.config_write import save_tier_credential, save_tier_slug
from tstd.context import PromptAssembler
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.provider import ChatCompletionRequest, ChatMessage, content_as_text
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools.delegate import DelegateRuntime, run_worker_child

_THINK_LOW: dict[str, Any] = {"chat_template_kwargs": {"reasoning_effort": "low"}}
_WORKER_EXTRA: dict[str, Any] = {**_THINK_LOW, "top_k": 20}
_THINK_OFF: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}
_MERGED: dict[str, Any] = {
    "chat_template_kwargs": {"reasoning_effort": "low", "enable_thinking": False},
    "top_k": 20,
}
_URL = "http://mock.local/v1"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The once-per-process set starts empty for every test."""
    monkeypatch.setattr(loop_module, "_VERDICT_CAP_WARNED", set())


def _tier(
    slug: str,
    extra_body: dict[str, Any] | None = None,
    verdict_extra_body: dict[str, Any] | None = None,
    max_output_tokens: int = 777,
) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=_URL,
        input_price=1.0,
        output_price=2.0,
        cache_read_price=0.5,
        context_window=100_000,
        max_output_tokens=max_output_tokens,
        extra_body=extra_body,
        verdict_extra_body=verdict_extra_body,
    )


def _config(verdict: dict[str, Any] | None = _THINK_OFF) -> ModelConfig:
    """Worker and brain both carry a verdict map; only verdicts may send it."""
    return ModelConfig(
        presets={
            "test": Preset(
                brain=_tier("test-brain", _THINK_LOW, verdict, max_output_tokens=1_234),
                worker=_tier("test-worker", _WORKER_EXTRA, verdict),
                validator=_tier("test-validator"),
            ),
        },
        active_preset="test",
    )


def _last_user_text(call: ChatCompletionRequest) -> str:
    return content_as_text(call.messages[-1].content) if call.messages else ""


def _cap_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "tstd.loop" and "verdict_extra_body" in record.getMessage()
    ]


async def _run_dod(
    tmp_path: Path, config: ModelConfig, worker: Script, *, max_iterations: int = 8
) -> tuple[Session, MockProvider]:
    """An unattended run whose DoD prose item is polled on the worker."""
    session = Session(str(tmp_path))
    charter = make_charter(max_iterations=max_iterations)
    _wire_autonomy(session, charter)
    mock = MockProvider(
        scripts={"test-brain": Script(kind="stream", content="Working"), "test-worker": worker}
    )
    runner = await start_loop(session, TierRouter(), mock, config)
    await session.add_user_message(first_prompt(charter))
    await wait_for_state(session, "complete")
    assert not runner.is_running
    return session, mock


# ── Config ──────────────────────────────────────────────────────────────


def test_verdict_extra_body_is_optional() -> None:
    assert _tier("m").verdict_extra_body is None


def test_verdict_extra_body_loads_nested_values(tmp_path: Path) -> None:
    extra = (
        "      verdict_extra_body:\n"
        "        chat_template_kwargs:\n"
        "          enable_thinking: false\n"
    )
    config = _load(tmp_path, _tier_yaml(extra))
    assert config.tier("worker").verdict_extra_body == _THINK_OFF


@pytest.mark.parametrize("key", sorted(EXTRA_BODY_RESERVED_KEYS))
def test_reserved_keys_are_a_load_error(tmp_path: Path, key: str) -> None:
    extra = f"      verdict_extra_body:\n        {key}: 1\n"
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tier_yaml(extra))
    message = str(caught.value)
    assert "presets.mine.brain.verdict_extra_body" in message
    assert f"verdict_extra_body cannot set {key}:" in message
    assert "TST Desk sends it itself" in message


def test_every_clash_is_named_once(tmp_path: Path) -> None:
    extra = "      verdict_extra_body: {stream: false, model: other, top_p: 0.9}\n"
    with pytest.raises(
        ConfigError, match="verdict_extra_body cannot set model, stream: TST Desk sends them"
    ):
        _load(tmp_path, _tier_yaml(extra))


@pytest.mark.parametrize(
    "value",
    ["{metadata: {run: 2026-10-09}}", "{seed: .nan}", "{at: 2026-10-09 14:00:00}"],
)
def test_values_that_are_not_json_are_a_load_error(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tier_yaml(f"      verdict_extra_body: {value}\n"))
    message = str(caught.value)
    assert "presets.mine.brain.verdict_extra_body" in message
    assert "verdict_extra_body must be plain JSON" in message


def test_verdict_extra_body_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="verdict_extra_body"):
        _load(tmp_path, _tier_yaml("      verdict_extra_body: off\n"))


def test_extra_body_messages_still_name_extra_body(tmp_path: Path) -> None:
    """Sharing the validators did not change TD-5008's wording."""
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tier_yaml("      extra_body: {max_tokens: 1}\n"))
    message = str(caught.value)
    assert "extra_body cannot set max_tokens:" in message
    assert "verdict_extra_body" not in message


_ROUND_TRIP = """\
presets:
  mine:
    brain:
      slug: test-brain
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
    worker:
      # Thinking off for the one-word verdicts only.
      verdict_extra_body:
        chat_template_kwargs:
          enable_thinking: false
      base_url: http://127.0.0.1:8000/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
      extra_body: {chat_template_kwargs: {reasoning_effort: low}}
    validator:
      slug: test-validator
      base_url: http://mock.local/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
active_preset: mine
"""


def test_settings_writes_keep_verdict_extra_body(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_ROUND_TRIP, encoding="utf-8")
    save_tier_slug("mine", "worker", "new-worker", path)
    save_tier_credential("mine", "worker", "openrouter", path)
    after = load_config(path)
    worker = after.tier("worker")
    assert worker.slug == "new-worker"
    assert worker.credential == "openrouter"
    assert worker.verdict_extra_body == _THINK_OFF
    assert worker.extra_body == _THINK_LOW
    assert "# Thinking off for the one-word verdicts only." in path.read_text(encoding="utf-8")


# ── Merge ───────────────────────────────────────────────────────────────


def test_nested_maps_merge() -> None:
    base = {"chat_template_kwargs": {"reasoning_effort": "low", "deep": {"a": 1}}, "top_k": 20}
    override = {"chat_template_kwargs": {"enable_thinking": False, "deep": {"b": 2}}}
    assert merge_extra_body(base, override) == {
        "chat_template_kwargs": {
            "reasoning_effort": "low",
            "enable_thinking": False,
            "deep": {"a": 1, "b": 2},
        },
        "top_k": 20,
    }


@pytest.mark.parametrize(
    ("base", "override", "expected"),
    [
        ({"k": {"x": 1}}, {"k": {"x": 2}}, {"k": {"x": 2}}),
        ({"k": {"x": 1}}, {"k": 5}, {"k": 5}),
        ({"k": 5}, {"k": {"x": 1}}, {"k": {"x": 1}}),
        ({"k": [1, 2]}, {"k": [3]}, {"k": [3]}),
        ({"k": "low"}, {"k": None}, {"k": None}),
    ],
)
def test_verdict_wins_a_leaf(
    base: dict[str, Any], override: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert merge_extra_body(base, override) == expected


def test_merge_changes_neither_input() -> None:
    base: dict[str, Any] = {"chat_template_kwargs": {"reasoning_effort": "low"}, "stop": ["x"]}
    override: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}, "ids": [1]}
    base_before, override_before = copy.deepcopy(base), copy.deepcopy(override)

    merged = merge_extra_body(base, override)
    assert base == base_before
    assert override == override_before

    merged["chat_template_kwargs"]["reasoning_effort"] = "high"
    merged["chat_template_kwargs"]["enable_thinking"] = True
    merged["stop"].append("y")
    merged["ids"].append(2)
    assert base == base_before
    assert override == override_before


def test_tier_verdict_extras_none_cases() -> None:
    assert _tier("m").verdict_extras() is None

    only_base = _tier("m", _WORKER_EXTRA)
    # The same object the verdict call sent before TD-5012.
    assert only_base.verdict_extras() is only_base.extra_body

    only_verdict = _tier("m", None, _THINK_OFF)
    extras = only_verdict.verdict_extras()
    assert extras == _THINK_OFF
    assert extras is not only_verdict.verdict_extra_body

    both = _tier("m", _WORKER_EXTRA, _THINK_OFF)
    assert both.verdict_extras() == _MERGED
    assert both.extra_body == _WORKER_EXTRA
    assert both.verdict_extra_body == _THINK_OFF


# ── Requests ────────────────────────────────────────────────────────────


def _assert_verdict_body(call: ChatCompletionRequest) -> None:
    body = call.to_dict()
    assert body["max_tokens"] == 8
    assert body["temperature"] == 0.0
    assert body["chat_template_kwargs"] == _MERGED["chat_template_kwargs"]
    assert body["top_k"] == 20


async def test_dod_poll_sends_the_merged_extras(tmp_path: Path) -> None:
    session, mock = await _run_dod(tmp_path, _config(), Script(kind="text", content="GREEN"))
    assert session.autonomy_stop_reason == DOD_MET
    polls = [
        call
        for call in mock.calls
        if call.model == "test-worker" and _last_user_text(call).endswith("Verdict:")
    ]
    assert polls
    for call in polls:
        _assert_verdict_body(call)


async def test_classifier_sends_the_merged_extras(tmp_path: Path) -> None:
    session, dispatcher = make_echo_session(tmp_path, BoundaryConfig())
    mock = MockProvider(
        scripts={"test-worker": Script(kind="text", content="A")},
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name="echo", tool_arguments='{"message": "hi"}'),
                Script(kind="stream", content="Done"),
            ]
        },
    )
    runner = await start_dispatch_loop(
        session, TierRouter(lead_turns=80), mock, _config(), None, dispatcher
    )
    try:
        await session.add_user_message("echo hi")
        await wait_for_turn(session, 1, 8.0)
    finally:
        await runner.cancel()

    classified = [
        call
        for call in mock.calls
        if call.model == "test-worker" and _last_user_text(call).endswith("Class:")
    ]
    assert len(classified) == 1
    _assert_verdict_body(classified[0])
    brain = [call.to_dict() for call in mock.calls if call.model == "test-brain"]
    assert brain
    assert all(body["chat_template_kwargs"] == _THINK_LOW["chat_template_kwargs"] for body in brain)


async def test_a_worker_tier_turn_sends_only_extra_body(tmp_path: Path) -> None:
    """Turn 2 runs on the worker, which has a verdict map; it does not ride."""
    mock = MockProvider(
        scripts={
            "test-brain": Script(kind="stream", content="hi"),
            "test-worker": Script(kind="stream", content="hi again"),
        }
    )
    session = Session(str(tmp_path))
    runner = await start_loop(session, TierRouter(lead_turns=1), mock, _config())
    try:
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
        await session.add_user_message("and again")
        await wait_for_turn(session, 2)
    finally:
        await runner.cancel()

    assert [call.model for call in mock.calls] == ["test-brain", "test-worker"]
    brain, worker = (call.to_dict() for call in mock.calls)
    assert brain["chat_template_kwargs"] == _THINK_LOW["chat_template_kwargs"]
    assert worker["stream"] is True
    assert worker["max_tokens"] == 777
    assert worker["chat_template_kwargs"] == _WORKER_EXTRA["chat_template_kwargs"]
    assert worker["top_k"] == 20
    assert "temperature" not in worker


async def test_delegate_sends_only_extra_body(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
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
        dispatcher=_parent_dispatcher(ws),
        assembler=PromptAssembler(parent.workspace_path),
        session_start=time.time(),
        iterations=0,
    )

    summary = await run_worker_child(parent=parent, task="Read notes.md and summarize")

    assert "hello from notes" in summary
    worker_calls = [call for call in mock.calls if call.model == "test-worker"]
    assert len(worker_calls) == 2
    for call in worker_calls:
        assert call.extra_body is config.tier("worker").extra_body
        body = call.to_dict()
        assert body["chat_template_kwargs"] == _WORKER_EXTRA["chat_template_kwargs"]
        assert body["max_tokens"] == 777


async def test_without_verdict_extra_body_the_request_is_unchanged(tmp_path: Path) -> None:
    config = _config(verdict=None)
    session, mock = await _run_dod(tmp_path, config, Script(kind="text", content="GREEN"))
    assert session.autonomy_stop_reason == DOD_MET
    worker_calls = [call for call in mock.calls if call.model == "test-worker"]
    assert worker_calls
    for call in worker_calls:
        # The TD-5008 request, built the way _worker_completion built it.
        before = ChatCompletionRequest(
            model="test-worker",
            messages=[ChatMessage(role="user", content=_last_user_text(call))],
            max_tokens=8,
            temperature=0.0,
            extra_body=config.tier("worker").extra_body,
        )
        assert json.dumps(call.to_dict()) == json.dumps(before.to_dict())
        assert call.extra_body is config.tier("worker").extra_body


# ── The cap warning ─────────────────────────────────────────────────────


async def test_an_empty_verdict_at_the_cap_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = Script(kind="text", content="", finish_reason="length")
    with caplog.at_level(logging.WARNING, logger="tstd.loop"):
        session, mock = await _run_dod(tmp_path, _config(verdict=None), empty, max_iterations=3)
    assert session.autonomy_stop_reason != DOD_MET

    polls = [call for call in mock.calls if call.model == "test-worker"]
    assert len(polls) >= 2
    warnings = _cap_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "worker tier" in message
    assert "probably spent the cap thinking" in message
    fields = getattr(warnings[0], "extra_fields", {})
    assert fields["tier"] == "worker"
    assert fields["base_url"] == _URL
    assert fields["model"] == "test-worker"
    assert fields["max_tokens"] == 8
    assert fields["verdict_extra_body_set"] is False


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [("GREEN", "length"), ("", "stop"), ("", None)],
)
async def test_no_warning_unless_empty_at_the_cap(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    content: str,
    finish_reason: str | None,
) -> None:
    script = Script(kind="text", content=content, finish_reason=finish_reason)
    with caplog.at_level(logging.WARNING, logger="tstd.loop"):
        session, mock = await _run_dod(tmp_path, _config(), script, max_iterations=2)
    assert any(call.model == "test-worker" for call in mock.calls)
    assert _cap_warnings(caplog) == []
    # A verdict cut at the cap after its answer is still a verdict.
    assert (session.autonomy_stop_reason == DOD_MET) is (content == "GREEN")


def test_the_warning_is_per_endpoint_and_model(caplog: pytest.LogCaptureFixture) -> None:
    first = _tier("forge")
    other_host = first.model_copy(update={"base_url": "http://other.local/v1"})
    with caplog.at_level(logging.WARNING, logger="tstd.loop"):
        loop_module._warn_verdict_cap_once(first, "forge")
        loop_module._warn_verdict_cap_once(first, "forge")
        loop_module._warn_verdict_cap_once(first, "other-model")
        loop_module._warn_verdict_cap_once(other_host, "forge")
        loop_module._warn_verdict_cap_once(other_host, "forge")
    keys = [
        (getattr(r, "extra_fields", {})["base_url"], getattr(r, "extra_fields", {})["model"])
        for r in _cap_warnings(caplog)
    ]
    assert keys == [
        (_URL, "forge"),
        (_URL, "other-model"),
        ("http://other.local/v1", "forge"),
    ]
