"""How a tier's ``extra_body`` meets the request (TD-5008).

It only fills in fields the request left unset, so a caller's explicit
value wins: the classifier's ``temperature`` 0.0 stays deterministic
whatever the worker tier asks for. Its values must be plain JSON, or the
config fails to load instead of every request failing later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_cap_enforcement import wait_for_state
from tests.test_loop import start_loop
from tests.test_request_extras import _load, _tier, _tier_yaml
from tstd.autonomy.runner import DOD_MET, first_prompt
from tstd.config import ConfigError, ModelConfig, Preset
from tstd.mock import MockProvider, Script
from tstd.provider import ChatCompletionRequest
from tstd.router import TierRouter
from tstd.session import Session

_HOT: dict[str, Any] = {"temperature": 1.5, "top_p": 0.9, "stop": ["</done>"], "top_k": 20}


def test_explicit_request_fields_win() -> None:
    body = ChatCompletionRequest(
        model="m",
        messages=[],
        max_tokens=8,
        temperature=0.0,
        top_p=0.5,
        stop=["\n"],
        extra_body=_HOT,
    ).to_dict()
    assert body["temperature"] == 0.0
    assert body["top_p"] == 0.5
    assert body["stop"] == ["\n"]
    assert body["max_tokens"] == 8
    assert body["top_k"] == 20


def test_extra_body_fills_in_unset_fields() -> None:
    body = ChatCompletionRequest(model="m", messages=[], extra_body=_HOT).to_dict()
    assert {key: body[key] for key in _HOT} == _HOT


async def test_classifier_keeps_temperature_zero(tmp_path: Path) -> None:
    config = ModelConfig(
        presets={
            "test": Preset(
                brain=_tier("test-brain", 1_000, {"temperature": 1.5}),
                worker=_tier("test-worker", 500, {"temperature": 1.5}),
                validator=_tier("test-validator", 999, None),
            ),
        },
        active_preset="test",
    )
    session = Session(str(tmp_path))
    charter = make_charter(max_iterations=8)
    _wire_autonomy(session, charter)
    mock = MockProvider(
        scripts={
            "test-brain": Script(kind="stream", content="Working"),
            "test-worker": Script(kind="text", content="GREEN"),
        }
    )
    runner = await start_loop(session, TierRouter(), mock, config)
    await session.add_user_message(first_prompt(charter))
    await wait_for_state(session, "complete")
    assert session.autonomy_stop_reason == DOD_MET
    assert not runner.is_running

    worker_calls = [call.to_dict() for call in mock.calls if call.model == "test-worker"]
    assert worker_calls
    assert all(body["temperature"] == 0.0 for body in worker_calls)
    brain_calls = [call.to_dict() for call in mock.calls if call.model == "test-brain"]
    assert brain_calls
    assert all(body["temperature"] == 1.5 for body in brain_calls)


@pytest.mark.parametrize(
    "value",
    ["{metadata: {run: 2026-10-09}}", "{seed: .nan}", "{at: 2026-10-09 14:00:00}"],
)
def test_values_that_are_not_json_are_a_load_error(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _tier_yaml(f"      extra_body: {value}\n"))
    message = str(caught.value)
    assert "presets.mine.brain.extra_body" in message
    assert "extra_body must be plain JSON" in message


def test_a_quoted_date_is_fine(tmp_path: Path) -> None:
    config = _load(tmp_path, _tier_yaml("      extra_body: {metadata: {run: '2026-10-09'}}\n"))
    assert config.tier("brain").extra_body == {"metadata": {"run": "2026-10-09"}}
