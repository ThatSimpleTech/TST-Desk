"""Trimmed tool output is labelled apart from compaction (TD-5150).

The loop sends ``context_compacted`` for two things: compaction, which
folds old turns into a summary, and the in-turn trim of this turn's tool
results (TD-5010), which removes no message. The trim now says so with
``trigger: "inflight_trim"``, so the window can show it as a quieter
"Trimmed tool output" row instead of one more compaction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from tests.test_compaction import pad, run_session, tiny_config
from tests.test_context_fit import _ELISION_NOTE, _forge_config, _run, _write_reads
from tests.test_docs_architecture_guide import _rows
from tests.test_loop import wait_for_turn
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted, parse_daemon_event
from tstd.provider import content_as_text
from tstd.session import Session

_FIXTURES = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "protocol-fixtures.json"


def _compactions(session: Session) -> list[ContextCompacted]:
    return [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]


async def test_the_in_turn_trim_is_labelled_inflight_trim(tmp_path: Path) -> None:
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="tool_call", tool_batch=_write_reads(tmp_path)),
                Script(kind="stream", content="Here is the page."),
            ]
        }
    )
    session = Session(str(tmp_path))
    runner = await _run(session, mock, _forge_config(), tmp_path)
    try:
        await session.add_user_message("Write an HTML page about the project")
        turn = await wait_for_turn(session, 1, 15.0)
    finally:
        await runner.cancel()

    assert turn.failed is False
    sent = list(mock.calls[1].messages)
    trimmed = [m for m in sent if m.role == "tool" and _ELISION_NOTE in content_as_text(m.content)]
    (event,) = _compactions(session)
    assert event.trigger == "inflight_trim"
    # The count is of tool results trimmed, and no message left.
    assert event.dropped_messages == len(trimmed) >= 1
    assert event.kept_messages == len(sent)
    assert event.tokens_before > event.tokens_after
    assert event.guidance is None


async def test_compaction_by_budget_stays_auto(tmp_path: Path) -> None:
    session, _mock, runner, _hashes = await run_session(
        tmp_path / "ws",
        tmp_path / "home",
        [pad("turn1", 800), pad("turn2", 800), pad("turn3", 800)],
        tiny_config(),
    )
    await runner.cancel()

    (event,) = _compactions(session)
    assert event.trigger == "auto"
    assert event.dropped_messages > 0


def test_the_trim_fixture_round_trips() -> None:
    fixtures = json.loads(_FIXTURES.read_text())
    raw = fixtures["context_compacted_inflight_trim"]
    event = parse_daemon_event(json.dumps(raw))
    assert isinstance(event, ContextCompacted)
    assert event.trigger == "inflight_trim"
    # Every field an older window reads is still there.
    assert {"dropped_messages", "kept_messages", "tokens_before", "tokens_after"} <= set(raw)


def test_an_event_without_a_trigger_still_reads_as_auto() -> None:
    raw = {
        "type": "context_compacted",
        "session_id": "s1",
        "dropped_messages": 2,
        "kept_messages": 3,
        "tokens_before": 900,
        "tokens_after": 400,
        "seq": 4,
    }
    event = parse_daemon_event(json.dumps(raw))
    assert isinstance(event, ContextCompacted)
    assert event.trigger == "auto"


def test_the_architecture_row_names_every_trigger() -> None:
    row = " ".join(_rows("daemon-events")["context_compacted"])
    triggers = get_args(ContextCompacted.model_fields["trigger"].annotation)
    assert "inflight_trim" in triggers
    for trigger in triggers:
        assert f"`{trigger}`" in row, f"architecture.md's context_compacted row omits {trigger}"
