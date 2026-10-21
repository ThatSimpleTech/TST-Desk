"""Per-run permissions for unattended runs (TD-5118).

``tst run --allow`` and ``--deny`` add policy rules for the one turn the
run starts, ``--on-ask deny-and-continue`` turns an approval into a
refusal the model reads instead of stopping the run, the classifier still
judges every call, and no flag lets a Class C call run without asking.
The end-to-end turns run against a real daemon on the mock provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_delegate import _bind_runtime, _parent_dispatcher
from tests.test_delegate import _workspace as _delegate_workspace
from tests.test_tst_run_scripts import (
    _json_line,
    _only_session_id,
    _start,
    _stop,
    _tool_turn,
    _workspace,
)
from tstd import cli, cli_run
from tstd.autonomy import AmbiguousClassifier, DecisionClass, DecisionClassifier
from tstd.cli_run import parse_rule, run_permissions
from tstd.cli_turn import (
    EXIT_APPROVAL_NEEDED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    RUN_PERMISSIONS_UNSUPPORTED,
    stream_turn,
)
from tstd.config import ModelConfig, ShellReadOnlyConfig
from tstd.loop import session_boundary
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, resolve_explained, with_run_rules
from tstd.protocol import (
    PROTOCOL_VERSION,
    ApprovalRequest,
    RunPermissions,
    RunRule,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from tstd.provider import ChatCompletionRequest, ProviderError, StreamChunk
from tstd.queued_messages import messages_from_rows, queue_rows
from tstd.session import QueuedUserMessage, Session
from tstd.tools import create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard
from tstd.tools.delegate import run_worker_child
from tstd.tools.dispatch import ToolDispatcher

DENY_AND_CONTINUE = RunPermissions(on_ask="deny_and_continue")


def _allow(*rules: str, on_ask: str = "prompt") -> dict[str, Any]:
    permissions = run_permissions([parse_rule(r) for r in rules], [], on_ask)
    assert permissions is not None
    return permissions


def _deny(*rules: str, on_ask: str = "prompt") -> dict[str, Any]:
    permissions = run_permissions([], [parse_rule(r) for r in rules], on_ask)
    assert permissions is not None
    return permissions


def _events(daemon: Any, sid: str, kind: type) -> list[Any]:
    session = daemon.session_registry.get(sid)
    assert session is not None
    return [e for e in session.event_log.all_events if isinstance(e, kind)]


# ── Command line ────────────────────────────────────────────────────────


class TestArgs:
    def test_rules_and_on_ask_reach_the_message(self) -> None:
        ns = cli.parse_args(
            [
                "run",
                "--workspace",
                ".",
                "--allow",
                "shell: npm test*",
                "--allow",
                "fs_write",
                "--deny",
                "web_fetch:*.example.com",
                "--on-ask",
                "deny-and-continue",
                "--message",
                "go",
            ]
        )
        assert run_permissions(ns.allow, ns.deny, ns.on_ask) == {
            "allow": [{"tool": "shell", "args": "npm test*"}, {"tool": "fs_write", "args": "**"}],
            "deny": [{"tool": "web_fetch", "args": "*.example.com"}],
            "on_ask": "deny_and_continue",
        }

    def test_a_plain_run_sends_no_permissions(self) -> None:
        ns = cli.parse_args(["run", "--workspace", ".", "--message", "go"])
        assert ns.on_ask == "prompt"
        assert run_permissions(ns.allow, ns.deny, ns.on_ask) is None

    def test_the_first_colon_splits_so_a_drive_letter_stays_in_args(self) -> None:
        assert parse_rule("fs_read:C:/work/*") == {"tool": "fs_read", "args": "C:/work/*"}

    @pytest.mark.parametrize(
        "argv",
        [
            ["--allow", ":npm test"],
            ["--allow", "shell:"],
            ["--deny", "my tool:x"],
            ["--deny", ""],
            ["--on-ask", "yes"],
        ],
    )
    def test_bad_values_are_usage_errors(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.parse_args(["run", "--workspace", ".", "--message", "go", *argv])
        assert exc.value.code == EXIT_USAGE


# ── The rules beside the workspace's ────────────────────────────────────


class TestRunRules:
    shell = create_registry().get("shell")
    write = create_registry().get("fs_write")

    def _decide(
        self,
        config: PolicyConfig,
        run: RunPermissions | None,
        command: str,
        decision_class: DecisionClass = DecisionClass.B,
    ) -> Any:
        assert self.shell is not None
        return resolve_explained(
            with_run_rules(config, run), self.shell, {"command": command}, decision_class
        )

    def test_an_allow_runs_a_b_call_and_names_the_run(self) -> None:
        run = RunPermissions(allow=[RunRule(tool="shell", args="npm test*")])
        decision = self._decide(PolicyConfig(), run, "npm test")
        assert decision.effect == "auto"
        assert "`shell: npm test*` (this run)" in decision.reason
        # Every command of a chained line still needs a rule (TD-5101).
        assert self._decide(PolicyConfig(), run, "npm test; curl x").effect == "ask"

    def test_deny_first_against_the_workspace_rules(self) -> None:
        workspace = PolicyConfig(
            rules=[
                PolicyRule(tool="shell", args="npm *", effect="ask"),
                PolicyRule(tool="shell", args="git *", effect="auto"),
                PolicyRule(tool="shell", args="rm *", effect="never"),
            ]
        )
        allow = RunPermissions(allow=[RunRule(tool="shell", args="*")])
        assert self._decide(workspace, allow, "npm test").effect == "ask"
        assert self._decide(workspace, allow, "rm -rf x").effect == "never"
        deny = RunPermissions(deny=[RunRule(tool="shell", args="git push*")])
        decision = self._decide(workspace, deny, "git push origin")
        assert decision.effect == "never"
        assert "(this run)" in decision.reason
        assert self._decide(workspace, deny, "git status").effect == "auto"

    @pytest.mark.parametrize("class_c_default", ["ask", "never"])
    def test_no_allow_makes_class_c_run(self, class_c_default: str) -> None:
        run = RunPermissions(allow=[RunRule(tool="*", args="**")])
        config = PolicyConfig(class_c_default=class_c_default)  # type: ignore[arg-type]
        decision = self._decide(config, run, "echo x > .envrc", DecisionClass.C)
        assert decision.effect == class_c_default
        assert "may not run automatically" in decision.reason

    def test_the_workspace_policy_is_left_as_it_was(self) -> None:
        config = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="*", effect="auto")])
        run = RunPermissions(
            allow=[RunRule(tool="shell")], deny=[RunRule(tool="fs_write", args="*.lock")]
        )
        merged = with_run_rules(config, run)
        assert len(merged.rules) == 3
        assert config.rules == [PolicyRule(tool="fs_read", args="*", effect="auto")]
        assert with_run_rules(config, None) is config


# ── End to end, through the daemon ──────────────────────────────────────


class _TouchEachTurn(MockProvider):
    """Every turn runs ``touch a`` once, then says it is done.

    Keyed on the conversation rather than a call sequence, because the
    daemon's own requests between turns (memory distill) would take a
    scripted step out of order.
    """

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        if request.messages[-1].role == "tool":
            self._default = Script(kind="stream", content="done")
        else:
            self._default = Script(
                kind="tool_call", tool_name="shell", tool_arguments='{"command": "touch a"}'
            )
        async for chunk in super().chat_completion_stream(request):
            yield chunk


class TestAllowAndDeny:
    async def test_an_allow_holds_for_its_run_and_no_other(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = _TouchEachTurn()
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, provider)
        try:
            first = await cli_run.run_turn(
                workspace, "make a", data_dir, permissions=_allow("shell:touch a")
            )
            sid = await _only_session_id(daemon)
            made = (workspace / "a").exists()
            (workspace / "a").unlink()
            second = await cli_run.run_turn(
                workspace, "make a again", data_dir, continue_last=True, output="json"
            )
            turns = _events(daemon, sid, UserTurn)
            calls = _events(daemon, sid, ToolCall)
        finally:
            await _stop(daemon, task)
        assert first == EXIT_OK
        assert made
        # The next run brought no rule, so the same command asks.
        assert second == EXIT_APPROVAL_NEEDED
        assert not (workspace / "a").exists()
        assert turns[0].run_permissions is not None
        assert turns[0].run_permissions.allow == [RunRule(tool="shell", args="touch a")]
        assert turns[1].run_permissions is None
        # The classifier judged both calls; the rule only decided the gate.
        assert [c.decision_class for c in calls] == ["B", "B"]
        assert _json_line(capsys.readouterr().out.splitlines()[-1])["status"] == "approval_needed"

    async def test_a_deny_refuses_what_would_have_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": "notes.txt", "content": "x"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(
                workspace, "write notes", data_dir, permissions=_deny("fs_write")
            )
            sid = await _only_session_id(daemon)
            results = _events(daemon, sid, ToolResult)
            calls = _events(daemon, sid, ToolCall)
        finally:
            await _stop(daemon, task)
        assert code == EXIT_REFUSED
        assert not (workspace / "notes.txt").exists()
        assert "refused: fs_write (policy_denied)" in capsys.readouterr().err
        assert results[0].error_code == "policy_denied"
        assert "`fs_write: **` (this run)" in results[0].output
        # An in-workspace write is Class A: without --deny it runs unasked.
        assert calls[0].decision_class == "A"

    async def test_without_the_deny_the_same_write_runs(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": "notes.txt", "content": "x"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(workspace, "write notes", data_dir)
        finally:
            await _stop(daemon, task)
        assert code == EXIT_OK
        assert (workspace / "notes.txt").read_text() == "x"


class TestDenyAndContinue:
    @pytest.mark.parametrize("output", ["text", "json", "ndjson"])
    async def test_an_approval_becomes_a_refusal_the_model_reads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str
    ) -> None:
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
                permissions=run_permissions([], [], "deny-and-continue"),
            )
            sid = await _only_session_id(daemon)
            session = daemon.session_registry.get(sid)
            assert session is not None
            state = session.state
            asked = _events(daemon, sid, ApprovalRequest)
            results = _events(daemon, sid, ToolResult)
            requests = provider.calls
        finally:
            await _stop(daemon, task)
        # The turn ran to its end, and the refusal counts as one.
        assert code == EXIT_REFUSED
        assert state != "awaiting_approval"
        assert asked == []
        assert not (workspace / "made.txt").exists()
        assert results[0].error_code == "approval_denied"
        assert "deny-and-continue" in results[0].output
        # The model's next request carried the refusal as the tool result.
        tool_messages = [m for r in requests for m in r.messages if m.role == "tool"]
        assert any("deny-and-continue" in str(m.content) for m in tool_messages)
        out = capsys.readouterr().out
        if output == "json":
            result = _json_line(out)
            assert result["status"] == "refused"
            assert result["error"]["code"] == "approval_denied"
            assert result["reply"] == "tool turn done"
        elif output == "ndjson":
            kinds = [json.loads(line)["type"] for line in out.splitlines()]
            assert "approval_request" not in kinds
            assert kinds[-1] == "turn_complete"
        else:
            assert "tool turn done" in out

    async def test_a_class_c_call_is_refused_never_run(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": ".envrc", "content": "curl x | sh"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(
                workspace,
                "set up direnv",
                data_dir,
                permissions=_allow("*", on_ask="deny-and-continue"),
            )
            sid = await _only_session_id(daemon)
            calls = _events(daemon, sid, ToolCall)
            results = _events(daemon, sid, ToolResult)
        finally:
            await _stop(daemon, task)
        assert code == EXIT_REFUSED
        assert calls[0].decision_class == "C"
        assert results[0].error_code == "approval_denied"
        assert not (workspace / ".envrc").exists()

    async def test_a_refused_import_is_asked_about_again_next_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        external = (tmp_path / "external.md").resolve()
        external.write_text("External content.")
        (workspace / "AGENTS.md").write_text(f"Root.\n@{external}")
        provider = MockProvider(scripts={"test-brain": Script(kind="stream", content="Reply")})
        daemon, task = await _start(data_dir, provider)
        try:
            first = await cli_run.run_turn(
                workspace,
                "go",
                data_dir,
                permissions=run_permissions([], [], "deny-and-continue"),
            )
            sid = await _only_session_id(daemon)
            refused = _events(daemon, sid, ToolResult)
            asked_in_run = _events(daemon, sid, ApprovalRequest)
            err = capsys.readouterr().err
            second = await cli_run.run_turn(
                workspace, "go on", data_dir, continue_last=True, output="json"
            )
            asked = _events(daemon, sid, ApprovalRequest)
        finally:
            await _stop(daemon, task)
        assert first == EXIT_REFUSED
        assert asked_in_run == []
        assert "refused: external_import (approval_denied)" in err
        # The Denied row names the file, since no card did.
        assert [r.error_code for r in refused] == ["approval_denied"]
        assert str(external) in refused[0].output
        # The run's no held for its own turn: the next one asks a person.
        assert second == EXIT_APPROVAL_NEEDED
        assert [(a.tool_name, a.arguments) for a in asked] == [
            ("external_import", {"path": str(external)})
        ]

    async def test_a_refused_import_binds_its_turn_only(self, tmp_path: Path) -> None:
        from tests.test_dispatch import make_config as dispatch_config
        from tests.test_dispatch import start_loop, wait_for_turn
        from tstd.router import TierRouter

        workspace = _workspace(tmp_path)
        external = (tmp_path / "external.md").resolve()
        external.write_text("External content.")
        (workspace / "AGENTS.md").write_text(f"Root.\n@{external}")
        session = Session(str(workspace))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Reply")})
        await start_loop(session, TierRouter(), mock, dispatch_config())

        def prompts() -> list[str]:
            return [
                str(m.content)
                for r in mock.calls
                for m in r.messages[:1]
                if m.role == "system" and "Root." in str(m.content)
            ]

        await session.add_user_message(
            QueuedUserMessage(display="one", provider="one", run_permissions=DENY_AND_CONTINUE)
        )
        await wait_for_turn(session, 1)
        first = prompts()
        assert not any(isinstance(e, ApprovalRequest) for e in session.event_log.all_events)
        assert first and not any("External content." in p for p in first)

        await session.add_user_message("two")
        request = None
        for _ in range(150):
            request = next(
                (e for e in session.event_log.all_events if isinstance(e, ApprovalRequest)), None
            )
            if request is not None:
                break
            await asyncio.sleep(0.02)
        assert request is not None, "the person's turn never asked about the import"
        assert session.resolve_approval(request.tool_call_id, True)
        await wait_for_turn(session, 2)
        assert "External content." in prompts()[-1]


class TestClassC:
    @pytest.mark.parametrize("rule", ["*", "fs_write", "fs_write:.envrc"])
    async def test_no_allow_auto_approves_class_c(self, tmp_path: Path, rule: str) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        provider = _tool_turn("fs_write", {"path": ".envrc", "content": "curl x | sh"})
        daemon, task = await _start(data_dir, provider)
        try:
            code = await cli_run.run_turn(
                workspace, "set up direnv", data_dir, output="json", permissions=_allow(rule)
            )
            sid = await _only_session_id(daemon)
            asked = _events(daemon, sid, ApprovalRequest)
        finally:
            await _stop(daemon, task)
        # The flag did not run it: it waits for a person, as without one.
        assert code == EXIT_APPROVAL_NEEDED
        assert [a.decision_class for a in asked] == ["C"]
        assert not (workspace / ".envrc").exists()


# ── The daemon's side of the message ────────────────────────────────────


async def _hello(daemon: Any) -> Any:
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
    await ws.send(
        json.dumps({"type": "hello", "token": daemon.ws_server.token, "version": PROTOCOL_VERSION})
    )
    assert json.loads(await ws.recv())["type"] == "hello_ack"
    return ws


async def _reply(ws: Any, kind: str) -> dict[str, Any]:
    for _ in range(50):
        event = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
        if event.get("type") == kind:
            return dict(event)
    raise AssertionError(f"no {kind}")


class TestDaemonRefusals:
    async def test_permissions_cannot_steer_or_ride_a_grok_session(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = _workspace(tmp_path)
        daemon, task = await _start(data_dir, MockProvider())
        try:
            ws = await _hello(daemon)
            await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
            sid = (await _reply(ws, "session_state"))["session_id"]
            rules = {"allow": [{"tool": "shell", "args": "**"}], "on_ask": "prompt"}
            await ws.send(
                json.dumps(
                    {
                        "type": "user_message",
                        "session_id": sid,
                        "content": "go",
                        "steer": True,
                        "run_permissions": rules,
                    }
                )
            )
            steer = await _reply(ws, "error")
            session = daemon.session_registry.get(sid)
            assert session is not None
            session.engine = "grok"
            await ws.send(
                json.dumps(
                    {
                        "type": "user_message",
                        "session_id": sid,
                        "content": "go",
                        "run_permissions": rules,
                    }
                )
            )
            grok = await _reply(ws, "error")
            await ws.close()
            turns = _events(daemon, sid, UserTurn)
        finally:
            await _stop(daemon, task)
        assert steer["code"] == "bad_request"
        assert "cannot steer" in steer["message"]
        assert grok["code"] == "run_rules_unsupported"
        assert turns == []


class _FakeWs:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = [json.dumps(e) for e in events]
        self.sent: list[dict[str, Any]] = []

    async def recv(self) -> str:
        if not self._events:
            await asyncio.sleep(10)
        return self._events.pop(0)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


async def test_a_daemon_that_ignores_the_permissions_has_its_turn_stopped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An older daemon drops the field; a --deny it never saw must not run."""
    ws = _FakeWs(
        [
            {"type": "user_turn", "seq": 1, "session_id": "s", "turn_id": "t", "content": "go"},
            {"type": "assistant_delta", "seq": 2, "session_id": "s", "delta": "too late"},
        ]
    )
    outcome = await stream_turn(ws, "s", None, 5.0, run_permissions=True)
    assert outcome.status == "error"
    assert outcome.exit_code == EXIT_ERROR
    assert outcome.error_code == RUN_PERMISSIONS_UNSUPPORTED
    assert ws.sent == [{"type": "stop_turn", "session_id": "s"}]
    assert RUN_PERMISSIONS_UNSUPPORTED in capsys.readouterr().err


async def test_an_echoed_turn_runs_on() -> None:
    echoed = DENY_AND_CONTINUE.model_dump()
    ws = _FakeWs(
        [
            {
                "type": "user_turn",
                "seq": 1,
                "session_id": "s",
                "turn_id": "t",
                "content": "go",
                "run_permissions": echoed,
            },
            {
                "type": "turn_complete",
                "seq": 2,
                "session_id": "s",
                "tokens": 1,
                "cost": 0.0,
                "tier": "brain",
                "duration": 0.1,
            },
        ]
    )
    outcome = await stream_turn(ws, "s", None, 5.0, output="json", run_permissions=True)
    assert outcome.status == "success"
    assert ws.sent == []


# ── The session's side ──────────────────────────────────────────────────


class TestSessionScope:
    async def test_an_import_approval_is_refused_without_a_card(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.run_permissions = DENY_AND_CONTINUE
        outcome = await session.request_import_approval(tmp_path.parent / "shared.md")
        assert not outcome.approved
        assert "deny-and-continue" in outcome.message
        events = session.event_log.all_events
        assert not any(isinstance(e, ApprovalRequest) for e in events)
        results = [e for e in events if isinstance(e, ToolResult)]
        assert [r.error_code for r in results] == ["approval_denied"]
        assert session.state != "awaiting_approval"

    async def test_a_grok_approval_is_refused_without_a_card(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.run_permissions = DENY_AND_CONTINUE
        outcome = await session.request_acp_approval(
            "tc-1", "run_command", {"command": "ls"}, summary="ls", reason="Grok asked"
        )
        assert not outcome.approved
        assert not any(isinstance(e, ApprovalRequest) for e in session.event_log.all_events)

    async def test_the_permissions_end_with_their_turn(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.run_permissions = DENY_AND_CONTINUE
        await session.event_log.add(
            TurnComplete(session_id=session.id, tokens=0, cost=0, tier="brain", duration=0, seq=1)
        )
        assert session.run_permissions is None

    def test_a_restart_never_brings_them_back(self) -> None:
        held = QueuedUserMessage(
            display="go", provider="go", run_permissions=RunPermissions(allow=[RunRule(tool="*")])
        )
        rows = queue_rows([held])
        assert "run_permissions" not in json.dumps(rows)
        assert messages_from_rows(rows)[0].run_permissions is None

    def test_a_restart_keeps_the_deny_rules(self) -> None:
        """TD-5161: dropping a deny widened the gate for the person's Send."""
        held = QueuedUserMessage(
            display="go",
            provider="go",
            run_permissions=RunPermissions(
                allow=[RunRule(tool="*")],
                deny=[RunRule(tool="fs_write")],
                on_ask="deny_and_continue",
            ),
        )
        back = messages_from_rows(json.loads(json.dumps(queue_rows([held]))))[0].run_permissions
        assert back == RunPermissions(deny=[RunRule(tool="fs_write")])

    def test_unreadable_deny_rules_drop_the_row(self) -> None:
        rows = queue_rows([QueuedUserMessage(display="go", provider="go")])
        rows[0]["deny"] = [{"tool": ""}]
        assert messages_from_rows(rows) == []


async def test_a_delegate_child_keeps_its_parents_run_rules(tmp_path: Path) -> None:
    ws = _delegate_workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    dispatcher.run_permissions_fn = lambda: parent.run_permissions
    parent.run_permissions = RunPermissions(deny=[RunRule(tool="fs_write")])
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments='{"path": "notes.md", "content": "changed"}',
                ),
                Script(kind="text", content="The write was refused."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    await run_worker_child(parent=parent, task="Rewrite notes.md")

    assert (ws / "notes.md").read_text(encoding="utf-8") == "hello from notes\n"


class _Counting:
    """A classifier that counts what it judged."""

    def __init__(self, inner: AmbiguousClassifier) -> None:
        self.inner = inner
        self.judged: list[str] = []

    async def classify(self, request: Any) -> Any:
        self.judged.append(request.tool_name)
        return await self.inner.classify(request)


async def _never_asked(*_args: object) -> Any:
    raise AssertionError("an allowed call asked")


async def test_the_classifier_judges_every_call_an_allow_lets_through(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    session = Session(str(tmp_path))
    session.run_permissions = RunPermissions(allow=[RunRule(tool="*")])
    boundary = session_boundary(session)

    async def worker(_prompt: str) -> str:
        return "B"

    counting = _Counting(
        AmbiguousClassifier(static=DecisionClassifier(boundary), call_worker=worker)
    )
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=counting,  # type: ignore[arg-type]
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(),
        approval_handler=_never_asked,
        workspace=tmp_path,
    )
    dispatcher.run_permissions_fn = lambda: session.run_permissions
    register_builtin_handlers(dispatcher)
    calls = [
        ("fs_read", {"path": "a.txt"}),
        ("fs_write", {"path": "b.txt", "content": "x"}),
        ("shell", {"command": "true"}),
    ]
    for i, (name, arguments) in enumerate(calls):
        result = await dispatcher.dispatch(f"tc{i}", name, arguments, session)
        assert result.status == "success", result.output
    assert counting.judged == ["fs_read", "fs_write", "shell"]


# ── A --deny over a read guards the read-only shell list too ────────────


def _read_only_config() -> ModelConfig:
    from tests.test_loop import make_config

    config = make_config()
    shell = config.shell.model_copy(update={"read_only": ShellReadOnlyConfig(enabled=True)})
    return config.model_copy(update={"shell": shell})


@pytest.mark.skipif(sys.platform == "win32", reason="the read-only list is POSIX only")
async def test_a_run_deny_on_a_file_keeps_cat_off_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    monkeypatch.setenv("PATH", os.pathsep.join(search))
    (tmp_path / "secret.txt").write_text("hunter2-token")
    session = Session(str(tmp_path))
    session.config = _read_only_config()
    boundary = session_boundary(session)

    async def worker(_prompt: str) -> str:
        return "B"

    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(static=DecisionClassifier(boundary), call_worker=worker),
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(),
        approval_handler=session.request_approval,
        workspace=tmp_path,
    )
    dispatcher.run_permissions_fn = lambda: session.run_permissions
    register_builtin_handlers(dispatcher)

    session.run_permissions = DENY_AND_CONTINUE
    unguarded = await dispatcher.dispatch("tc1", "shell", {"command": "cat secret.txt"}, session)
    session.run_permissions = RunPermissions(
        deny=[RunRule(tool="fs_read", args="secret.txt")], on_ask="deny_and_continue"
    )
    guarded = await dispatcher.dispatch("tc2", "shell", {"command": "cat secret.txt"}, session)

    # The list runs cat unasked; the run's rule over the file makes it ask,
    # and this run answers every ask with no.
    assert unguarded.status == "success"
    assert "hunter2-token" in unguarded.output
    assert guarded.error_code == "approval_denied"
    assert "hunter2-token" not in guarded.output


def test_only_this_computer_sets_a_turns_permissions() -> None:
    """A phone or another computer approves call by call; it never pre-approves."""
    from tstd.peer_policy import LOOPBACK_PLAIN, PeerView, check
    from tstd.protocol import parse_client_message

    def message(sid: str) -> Any:
        return parse_client_message(
            json.dumps(
                {
                    "type": "user_message",
                    "session_id": sid,
                    "content": "go",
                    "run_permissions": {"allow": [{"tool": "*"}]},
                }
            )
        )

    assert check(message("s1"), LOOPBACK_PLAIN, bridged=False) is None
    peers = [
        PeerView(remote=True, machine=False, viewer=True),
        PeerView(remote=True, machine=False, viewer=False),
        PeerView(remote=True, machine=True, viewer=False),
        PeerView(remote=False, machine=True, viewer=False),
    ]
    for peer in peers:
        refusal = check(message("s1"), peer, bridged=False)
        assert refusal is not None and json.loads(refusal)["code"] == "loopback_only", peer
    bridged = check(message("s1:mch_0123456789ab"), PeerView(False, False, True), bridged=True)
    assert bridged is not None and json.loads(bridged)["code"] == "loopback_only"
    plain = parse_client_message(
        json.dumps({"type": "user_message", "session_id": "s1", "content": "go"})
    )
    assert check(plain, peers[2], bridged=False) is None
