"""``tst run`` for scripts (TD-5111).

Existing sessions (``--session``, ``--continue``), one JSON result or an
NDJSON stream of the protocol events, the message on stdin, and exit
codes that tell success, refusal, approval needed and error apart. The
turns run against a real daemon on the mock provider.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_loop import make_config
from tstd import cli, cli_run
from tstd.cli_run import RunError, UsageError, pick_session, read_message
from tstd.cli_turn import (
    EXIT_APPROVAL_NEEDED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
)
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete, UserTurn
from tstd.provider import ChatCompletionRequest, ProviderError, StreamChunk


class _Echo(MockProvider):
    """Answers each turn with its own message, so two turns read apart."""

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        last = next(m for m in reversed(request.messages) if m.role == "user")
        self._default = Script(kind="stream", content=f"echo: {last.content}")
        async for chunk in super().chat_completion_stream(request):
            yield chunk


class _Stdin(io.StringIO):
    def __init__(self, text: str, *, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


async def _start(data_dir: Path, provider: MockProvider) -> tuple[Daemon, asyncio.Task[None]]:
    await asyncio.to_thread(data_dir.mkdir, exist_ok=True)
    daemon = Daemon(data_dir=data_dir, provider=provider)
    daemon.config = make_config()
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop(daemon: Daemon, task: asyncio.Task[None]) -> None:
    daemon._shutdown_event.set()
    await asyncio.wait_for(task, timeout=10.0)


def _workspace(tmp_path: Path, name: str = "workspace") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path.resolve()


async def _only_session_id(daemon: Daemon) -> str:
    sessions = await daemon.session_registry.list_sessions()
    assert len(sessions) == 1
    return sessions[0].id


def _tool_turn(tool: str, arguments: dict[str, Any]) -> MockProvider:
    return MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name=tool, tool_arguments=json.dumps(arguments)),
                Script(kind="stream", content="tool turn done"),
            ]
        }
    )


def _json_line(out: str) -> dict[str, Any]:
    lines = out.splitlines()
    assert len(lines) == 1, out
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


# ── Command line ────────────────────────────────────────────────────────


class TestArgs:
    def test_session_needs_no_workspace(self) -> None:
        ns = cli.parse_args(["run", "--session", "abc", "--message", "hi"])
        assert ns.session == "abc"
        assert ns.workspace is None
        assert ns.continue_last is False
        assert ns.output == "text"

    def test_continue_and_outputs(self, tmp_path: Path) -> None:
        for output in ("text", "json", "ndjson"):
            ns = cli.parse_args(
                ["run", "--workspace", str(tmp_path), "--continue", "--output", output]
            )
            assert ns.continue_last is True
            assert ns.output == output
            assert ns.message is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["run", "--message", "hi"],
            ["run", "--continue", "--message", "hi"],
            ["run", "--workspace", ".", "--session", "a", "--continue"],
            ["run", "--workspace", ".", "--output", "xml"],
        ],
    )
    def test_usage_errors_exit_2(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.parse_args(argv)
        assert exc.value.code == EXIT_USAGE


class TestMessageFromStdin:
    def test_message_flag_does_not_read_stdin(self) -> None:
        stdin = _Stdin("ignored", tty=False)
        assert read_message("hi", stdin) == "hi"
        assert stdin.read() == "ignored"

    def test_piped_stdin_is_the_message(self) -> None:
        assert read_message(None, _Stdin("from a pipe\n", tty=False)) == "from a pipe"
        assert read_message("-", _Stdin("two\nlines\r\n", tty=False)) == "two\nlines"

    def test_a_terminal_is_read_only_when_asked(self) -> None:
        with pytest.raises(UsageError):
            read_message(None, _Stdin("typed", tty=True))
        assert read_message("-", _Stdin("typed\n", tty=True)) == "typed"

    def test_empty_stdin_is_refused(self) -> None:
        with pytest.raises(UsageError):
            read_message(None, _Stdin("\n", tty=False))

    def test_no_stdin_at_all_is_a_usage_error(self) -> None:
        with pytest.raises(UsageError):
            read_message(None, None)
        assert read_message("hi", None) == "hi"

    def test_main_sends_stdin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        async def fake_run(
            workspace: Path | None, message: str, data_dir: Path, **kwargs: Any
        ) -> int:
            seen.append((message, kwargs))
            return 0

        monkeypatch.setattr(cli_run, "run_turn", fake_run)
        monkeypatch.setattr(cli_run.sys, "stdin", _Stdin("piped message\n", tty=False))
        argv = ["run", "--workspace", str(tmp_path), "--data-dir", str(tmp_path)]
        assert cli.main([*argv, "--output", "json", "--continue"]) == 0
        message, kwargs = seen[-1]
        assert message == "piped message"
        assert kwargs["output"] == "json"
        assert kwargs["continue_last"] is True
        assert kwargs["session_id"] is None

    def test_main_on_a_terminal_without_message_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def never(*_: Any, **__: Any) -> int:
            raise AssertionError("must not run")

        monkeypatch.setattr(cli_run, "run_turn", never)
        monkeypatch.setattr(cli_run.sys, "stdin", _Stdin("", tty=True))
        code = cli.main(["run", "--workspace", str(tmp_path), "--data-dir", str(tmp_path)])
        assert code == EXIT_USAGE
        assert "--message" in capsys.readouterr().err


# ── Choosing an existing session ────────────────────────────────────────


def _row(sid: str, workspace: Path, updated: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": sid,
        "workspace_path": str(workspace),
        "created_at": "2026-10-20T08:00:00+00:00",
        "updated_at": updated,
        "event_count": 3,
        **extra,
    }


class TestPickSession:
    def test_by_id(self, tmp_path: Path) -> None:
        rows = [_row("a", tmp_path, "2026-10-20T09:00:00+00:00")]
        assert pick_session(rows, session_id="a", workspace=None)["session_id"] == "a"
        assert pick_session(rows, session_id="a", workspace=tmp_path)["session_id"] == "a"

    def test_unknown_id_and_other_workspace_are_refused(self, tmp_path: Path) -> None:
        other = _workspace(tmp_path, "other")
        rows = [_row("a", tmp_path, "2026-10-20T09:00:00+00:00")]
        with pytest.raises(RunError) as missing:
            pick_session(rows, session_id="nope", workspace=None)
        assert missing.value.code == "session_not_found"
        with pytest.raises(RunError) as mismatch:
            pick_session(rows, session_id="a", workspace=other)
        assert mismatch.value.code == "workspace_mismatch"

    def test_continue_takes_the_newest_local_unarchived_session_here(self, tmp_path: Path) -> None:
        here = _workspace(tmp_path, "here")
        there = _workspace(tmp_path, "there")
        rows = [
            # The daemon sorts starred first; the newest still wins.
            _row("old-starred", here, "2026-10-20T09:00:00+00:00", starred=True),
            _row("newest-here", here, "2026-10-20T10:00:00.5+00:00"),
            _row("newer-there", there, "2026-10-20T11:00:00+00:00"),
            _row("archived", here, "2026-10-20T12:00:00+00:00", archived=True),
            _row("remote", here, "2026-10-20T13:00:00+00:00", machine="mch_1"),
        ]
        row = pick_session(rows, session_id=None, workspace=here)
        assert row["session_id"] == "newest-here"

    def test_continue_with_nothing_to_continue(self, tmp_path: Path) -> None:
        with pytest.raises(RunError) as exc:
            pick_session([], session_id=None, workspace=tmp_path)
        assert exc.value.code == "no_session_to_continue"

    def test_a_session_mid_turn_is_refused_not_queued(self, tmp_path: Path) -> None:
        rows = [_row("a", tmp_path, "2026-10-20T09:00:00+00:00", busy=True)]
        with pytest.raises(RunError) as exc:
            pick_session(rows, session_id="a", workspace=None)
        assert exc.value.code == "session_busy"
        with pytest.raises(RunError):
            pick_session(rows, session_id=None, workspace=tmp_path)


# ── Sending into an existing session ────────────────────────────────────


class TestExistingSession:
    async def test_session_flag_sends_into_that_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _Echo()
        daemon, task = await _start(data_dir, provider)
        try:
            assert await cli_run.run_turn(workspace, "first", data_dir) == EXIT_OK
            sid = await _only_session_id(daemon)
            capsys.readouterr()
            code = await cli_run.run_turn(None, "second", data_dir, session_id=sid)
            assert await _only_session_id(daemon) == sid
            session = daemon.session_registry.get(sid)
            assert session is not None
            turns = [e.content for e in session.event_log.all_events if isinstance(e, UserTurn)]
            # The request that answered "second", before shutdown's distill call.
            sent = [str(m.content) for m in provider.calls[-1].messages if m.role == "user"]
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        # Only this turn's reply: the first turn is not replayed.
        assert capsys.readouterr().out == "echo: second\n"
        assert turns == ["first", "second"]
        # The model saw the earlier turn: it is one conversation.
        assert sent[-2:] == ["first", "second"]

    async def test_continue_sends_into_the_workspace_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            assert await cli_run.run_turn(workspace, "first", data_dir) == EXIT_OK
            sid = await _only_session_id(daemon)
            capsys.readouterr()
            code = await cli_run.run_turn(workspace, "again", data_dir, continue_last=True)
            assert await _only_session_id(daemon) == sid
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        assert capsys.readouterr().out == "echo: again\n"

    async def test_continue_with_no_session_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            code = await cli_run.run_turn(workspace, "hi", data_dir, continue_last=True)
            sessions = await daemon.session_registry.list_sessions()
        finally:
            await _stop(daemon, task)
        assert code == EXIT_ERROR
        assert sessions == []
        assert "no_session_to_continue" in capsys.readouterr().err

    async def test_unknown_session_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        daemon, task = await _start(data_dir, _Echo())
        try:
            code = await cli_run.run_turn(None, "hi", data_dir, session_id="nope", output="json")
        finally:
            await _stop(daemon, task)
        assert code == EXIT_ERROR
        captured = capsys.readouterr()
        result = _json_line(captured.out)
        assert result["status"] == "error"
        assert result["error"]["code"] == "session_not_found"
        # stdout holds only the object; the person still reads why on stderr.
        assert "session_not_found" in captured.err


# ── --output json ───────────────────────────────────────────────────────


class TestJsonOutput:
    async def test_one_object_with_reply_session_cost_status_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            code = await cli_run.run_turn(workspace, "hi", data_dir, output="json")
            sid = await _only_session_id(daemon)
            session = daemon.session_registry.get(sid)
            assert session is not None
            done = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        result = _json_line(capsys.readouterr().out)
        assert set(result) == {"reply", "session_id", "cost", "status", "error"}
        assert result["status"] == "success"
        assert result["reply"] == "echo: hi"
        assert result["session_id"] == sid
        assert result["error"] is None
        assert len(done) == 1
        assert done[0].cost > 0
        assert result["cost"] == done[0].cost

    async def test_a_failed_turn_is_one_error_object(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        failing = MockProvider(
            default=Script(kind="error", error_code="auth_failed", status_code=401, content="no")
        )
        daemon, task = await _start(data_dir, failing)
        try:
            code = await cli_run.run_turn(workspace, "hi", data_dir, output="json")
        finally:
            await _stop(daemon, task)
        assert code == EXIT_ERROR
        result = _json_line(capsys.readouterr().out)
        assert result["status"] == "error"
        assert result["error"]["code"] == "auth_failed"
        assert result["session_id"]

    async def test_no_daemon_is_still_one_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]

        async def gone(_: Path) -> dict[str, Any]:
            return {"port": closed_port, "token": "t", "pid": 1}

        monkeypatch.setattr(cli_run, "resolve_daemon", gone)
        code = await cli_run.run_turn(tmp_path, "hi", tmp_path, output="json")
        assert code == EXIT_ERROR
        result = _json_line(capsys.readouterr().out)
        assert result["status"] == "error"
        assert result["error"]["code"] == "connection_error"
        assert result["session_id"] is None


# ── --output ndjson ─────────────────────────────────────────────────────


class TestNdjsonOutput:
    async def test_streams_the_sessions_protocol_events(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, _Echo())
        try:
            code = await cli_run.run_turn(workspace, "hi", data_dir, output="ndjson")
            sid = await _only_session_id(daemon)
            capsys_first = capsys.readouterr().out
            code_again = await cli_run.run_turn(
                None, "more", data_dir, session_id=sid, output="ndjson"
            )
            second = capsys.readouterr().out
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        assert code_again == EXIT_OK
        events = [json.loads(line) for line in capsys_first.splitlines()]
        assert all(e["session_id"] == sid for e in events)
        types = [e["type"] for e in events]
        assert {"session_state", "user_turn", "assistant_delta", "cost_update"} <= set(types)
        assert types[-1] == "turn_complete"
        assert "".join(e["delta"] for e in events if e["type"] == "assistant_delta") == "echo: hi"
        # The seq the daemon stamped rides along unchanged.
        assert all(isinstance(e["seq"], int) for e in events)
        # Continuing streams only the new turn, from its own user_turn.
        later = [json.loads(line) for line in second.splitlines()]
        assert [e["content"] for e in later if e["type"] == "user_turn"] == ["more"]
        assert later[0]["type"] == "user_turn"
        assert later[-1]["type"] == "turn_complete"
        assert min(e["seq"] for e in later) > max(e["seq"] for e in events)


# ── Exit codes ──────────────────────────────────────────────────────────


class TestExitCodes:
    def test_the_four_outcomes_have_their_own_codes(self) -> None:
        codes = {EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_APPROVAL_NEEDED}
        assert len(codes) == 4
        assert EXIT_USAGE not in codes

    async def test_a_refused_call_exits_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": "AGENTS.md", "content": "x"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(workspace, "edit the rules", data_dir)
        finally:
            await _stop(daemon, task)
        assert code == EXIT_REFUSED
        captured = capsys.readouterr()
        assert "tool turn done" in captured.out
        assert "refused: fs_write (boundary_refusal)" in captured.err
        assert not (workspace / "AGENTS.md").exists()

    async def test_a_refusal_in_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": "AGENTS.md", "content": "x"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(workspace, "edit the rules", data_dir, output="json")
        finally:
            await _stop(daemon, task)
        assert code == EXIT_REFUSED
        result = _json_line(capsys.readouterr().out)
        assert result["status"] == "refused"
        assert result["error"]["code"] == "boundary_refusal"
        assert result["reply"] == "tool turn done"

    @pytest.mark.parametrize("output", ["text", "json", "ndjson"])
    async def test_an_unanswerable_approval_exits_approval_needed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        output: str,
    ) -> None:
        monkeypatch.setattr(cli_run.sys, "stdin", _Stdin("y\n", tty=False))
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("shell", {"command": "touch made.txt"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(
                workspace,
                "make a file",
                data_dir,
                output=output,  # type: ignore[arg-type]
            )
            sid = await _only_session_id(daemon)
            session = daemon.session_registry.get(sid)
            assert session is not None
            # The card is logged a moment before the state flips; poll.
            for _ in range(100):
                if session.state == "awaiting_approval":
                    break
                await asyncio.sleep(0.02)
            state = session.state
        finally:
            await _stop(daemon, task)
        assert code == EXIT_APPROVAL_NEEDED
        # The turn waits for the window; nothing ran and nothing was approved.
        assert state == "awaiting_approval"
        assert not (workspace / "made.txt").exists()
        out = capsys.readouterr().out
        if output == "json":
            result = _json_line(out)
            assert result["status"] == "approval_needed"
            assert result["error"]["code"] == "approval_needed"
        if output == "ndjson":
            last = json.loads(out.splitlines()[-1])
            assert last["type"] == "approval_request"


def test_cli_run_never_binds() -> None:
    for module in (cli_run, cli):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "0.0.0.0" not in source
        assert "from websockets.asyncio.server" not in source
