"""``tst run --session`` / ``--continue`` against what the daemon really logs (TD-5111).

Two review findings on the first cut: a message the daemon's redaction
changes was never recognised as the run's own turn, so the run waited out
its whole budget and reported a timeout; and ``--continue`` chose by an
``updated_at`` that a turn never moved, and could choose a session that
had ended.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_tst_run_scripts import _Echo, _json_line, _row, _start, _stop, _workspace
from tstd import cli_run, cli_turn
from tstd.cli_run import ENDED_STATES, RunError, pick_session
from tstd.cli_turn import EXIT_OK, is_sent_message
from tstd.logging import redact_secrets
from tstd.protocol import UserTurn
from tstd.session import TERMINAL_STATES
from tstd.session_store import SessionStore

# Secret-shaped by the daemon's patterns, though only the last two are
# secrets: ``sk-`` has no word boundary, so ordinary names match too.
_REDACTED_MESSAGES = [
    "rename the task-management-dashboard component",
    "pin flask-sqlalchemy-migrate and tst-desk-session-survival",
    "rotate ghp_" + "a" * 36 + " today",
    "AKIA" + "B" * 16 + "\n" + "AKIA" + "C" * 16,
]


def _turns(daemon: Any, sid: str) -> list[str]:
    session = daemon.session_registry.get(sid)
    assert session is not None
    return [e.content for e in session.event_log.all_events if isinstance(e, UserTurn)]


async def _session_ids(daemon: Any) -> list[str]:
    return [s.id for s in await daemon.session_registry.list_sessions()]


class TestIsSentMessage:
    def test_the_same_text(self) -> None:
        assert is_sent_message("hello", "hello")
        assert not is_sent_message("hello", "hello there")

    @pytest.mark.parametrize("message", _REDACTED_MESSAGES)
    def test_the_daemon_redaction_of_it(self, message: str) -> None:
        logged = redact_secrets(message)
        assert logged != message
        assert is_sent_message(logged, message)

    def test_another_message_is_not_it(self) -> None:
        sent = "rename the task-management-dashboard component"
        assert not is_sent_message("rename the ta[REDACTED] widget", sent)
        assert not is_sent_message("move the ta[REDACTED] component", sent)
        assert not is_sent_message("[REDACTED] x [REDACTED]", "a x")
        # Each marker stood for at least one character.
        assert not is_sent_message("a[REDACTED]b", "ab")
        assert not is_sent_message("a[REDACTED][REDACTED]b", "axb")
        assert is_sent_message("a[REDACTED][REDACTED]b", "axyb")

    def test_a_message_that_says_redacted_itself(self) -> None:
        assert is_sent_message("see [REDACTED] here", "see [REDACTED] here")


class TestContinuePicksALiveSession:
    def test_ended_sessions_are_skipped(self, tmp_path: Path) -> None:
        rows = [
            _row(f"ended-{state}", tmp_path, f"2026-10-20T1{i}:00:00+00:00", state=state)
            for i, state in enumerate(sorted(ENDED_STATES))
        ]
        rows.append(_row("live", tmp_path, "2026-10-20T09:00:00+00:00", state="running"))
        assert pick_session(rows, session_id=None, workspace=tmp_path)["session_id"] == "live"

    def test_only_ended_sessions_is_nothing_to_continue(self, tmp_path: Path) -> None:
        rows = [_row("gone", tmp_path, "2026-10-20T09:00:00+00:00", state="cancelled")]
        with pytest.raises(RunError) as exc:
            pick_session(rows, session_id=None, workspace=tmp_path)
        assert exc.value.code == "no_session_to_continue"

    def test_the_cli_set_is_the_daemon_set(self) -> None:
        assert ENDED_STATES == TERMINAL_STATES


class TestStoreTouch:
    async def test_touch_moves_updated_at_only(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        await store.upsert("s1", str(tmp_path), "running")
        before = store.get("s1")
        assert before is not None
        created, updated = before.created_at, before.updated_at
        await store.touch("s1")
        await store.touch("unknown")
        record = SessionStore(tmp_path).get("s1")
        assert record is not None
        assert record.updated_at > updated
        assert record.created_at == created
        assert record.state == "running"


class TestRedactedMessage:
    async def test_a_redacted_message_is_still_this_runs_turn(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Short, so the old code's miss fails fast instead of after 900 s.
        monkeypatch.setattr(cli_turn, "_turn_timeout_secs", lambda *_a, **_k: 8.0)
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        message = _REDACTED_MESSAGES[0]
        daemon, task = await _start(data_dir, _Echo())
        try:
            assert await cli_run.run_turn(workspace, "first", data_dir) == EXIT_OK
            (sid,) = await _session_ids(daemon)
            capsys.readouterr()
            code = await cli_run.run_turn(None, message, data_dir, session_id=sid, output="json")
            turns = _turns(daemon, sid)
        finally:
            await _stop(daemon, task)
        result = _json_line(capsys.readouterr().out)
        assert code == EXIT_OK, result
        assert result["status"] == "success"
        assert result["error"] is None
        assert result["reply"].startswith("echo: rename the ta")
        assert turns == ["first", redact_secrets(message)]

    async def test_ndjson_starts_at_the_redacted_user_turn(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli_turn, "_turn_timeout_secs", lambda *_a, **_k: 8.0)
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        message = _REDACTED_MESSAGES[2]
        daemon, task = await _start(data_dir, _Echo())
        try:
            assert await cli_run.run_turn(workspace, "first", data_dir) == EXIT_OK
            capsys.readouterr()
            code = await cli_run.run_turn(
                workspace, message, data_dir, continue_last=True, output="ndjson"
            )
        finally:
            await _stop(daemon, task)
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert code == EXIT_OK
        assert events[0]["type"] == "user_turn"
        assert events[0]["content"] == redact_secrets(message)
        assert events[-1]["type"] == "turn_complete"


class TestContinueFollowsUse:
    async def test_continue_goes_to_the_session_used_last(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            assert await cli_run.run_turn(workspace, "A first", data_dir) == EXIT_OK
            (a,) = await _session_ids(daemon)
            assert await cli_run.run_turn(workspace, "B first", data_dir) == EXIT_OK
            (b,) = [s for s in await _session_ids(daemon) if s != a]
            # Work goes on in the older session.
            assert await cli_run.run_turn(None, "A second", data_dir, session_id=a) == EXIT_OK
            code = await cli_run.run_turn(workspace, "A third", data_dir, continue_last=True)
            a_turns, b_turns = _turns(daemon, a), _turns(daemon, b)
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        assert a_turns == ["A first", "A second", "A third"]
        assert b_turns == ["B first"]

    async def test_continue_skips_a_cancelled_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            assert await cli_run.run_turn(workspace, "A first", data_dir) == EXIT_OK
            (a,) = await _session_ids(daemon)
            assert await cli_run.run_turn(workspace, "B first", data_dir) == EXIT_OK
            (b,) = [s for s in await _session_ids(daemon) if s != a]
            # Cancelling moves A's updated_at past B's.
            await daemon.session_registry.cancel(a)
            capsys.readouterr()
            code = await cli_run.run_turn(
                workspace, "B second", data_dir, continue_last=True, output="json"
            )
            b_turns = _turns(daemon, b)
        finally:
            await _stop(daemon, task)
        result = _json_line(capsys.readouterr().out)
        assert code == EXIT_OK, result
        assert result["session_id"] == b
        assert b_turns == ["B first", "B second"]
