"""Hook runner (TD-5134).

Hooks come from user config, and from a workspace config only once it is
trusted. A hook answers with its exit code or JSON: it can add context,
block with a reason, or (before a tool) turn an automatic run into an
approval. It can never loosen a decision or skip the classifier. Every
run lands in the audit DB, redacted, and the inspector lists the active
hooks with their last results.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_shell_tools import make_shell_dispatcher
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.hooks import HookRunner, interpret
from tstd.hooks_config import HookEntry, HooksConfig, HooksConfigError, hooks_from_mapping
from tstd.logging import register_secret_value
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule
from tstd.protocol import (
    AssistantDelta,
    HookRun,
    HooksState,
    TurnComplete,
    UserTurn,
    parse_daemon_event,
)
from tstd.provider import content_as_text
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import ToolDispatcher
from tstd.workspace_trust import TrustStore, read_view

# ── Helpers ──────────────────────────────────────────────────────────────


def _script(tmp_path: Path, name: str, body: str) -> str:
    """A hook command running *body* as a Python script under this interpreter."""
    path = tmp_path / "hooks-src" / f"{name}.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _answer(tmp_path: Path, name: str, answer: dict[str, Any] | None = None, code: int = 0) -> str:
    """A hook that records its stdin next to itself and answers *answer*."""
    seen = tmp_path / "hooks-src" / f"{name}.stdin.json"
    printed = "" if answer is None else json.dumps(answer)
    body = (
        "import sys, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())\n"
        f"sys.stdout.write({printed!r})\n"
        f"sys.stderr.write('stderr says no')\n"
        f"sys.exit({code})\n"
    )
    return _script(tmp_path, name, body)


def _stdin(tmp_path: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (tmp_path / "hooks-src" / f"{name}.stdin.json").read_text(encoding="utf-8")
    )
    return data


def _runs(session: Session) -> list[HookRun]:
    return [e for e in session.event_log.all_events if isinstance(e, HookRun)]


def _hooked_session(tmp_path: Path, **events: list[HookEntry]) -> Session:
    session = Session(str(tmp_path))
    session.hooks.user = HooksConfig(**events)
    return session


def _dispatcher(tmp_path: Path, asked: list[str]) -> ToolDispatcher:
    dispatcher = make_shell_dispatcher(tmp_path)

    async def _record(
        _call_id: str, _tool: object, _args: object, _cls: object, _summary: str, reason: str
    ) -> ApprovalOutcome:
        asked.append(reason)
        return ApprovalOutcome(True, "")

    dispatcher.approval_handler = _record
    return dispatcher


def _write_workspace_hooks(workspace: Path, command: str) -> None:
    (workspace / ".tst").mkdir(exist_ok=True)
    (workspace / ".tst" / "config.yaml").write_text(
        yaml.safe_dump({"hooks": {"before_tool": [{"command": command}]}}), encoding="utf-8"
    )


# ── Config ───────────────────────────────────────────────────────────────


class TestConfig:
    def test_user_config_carries_hooks(self) -> None:
        config = make_config()
        assert config.hooks == HooksConfig()
        loaded = type(config).model_validate(
            {
                **config.model_dump(),
                "hooks": {"before_tool": [{"command": "true", "tools": ["shell"]}]},
            }
        )
        assert loaded.hooks.before_tool[0].tools == ["shell"]
        assert loaded.hooks.before_tool[0].timeout_seconds == 30

    def test_tools_only_on_tool_events(self, tmp_path: Path) -> None:
        with pytest.raises(HooksConfigError, match="only before_tool and after_tool"):
            hooks_from_mapping(
                tmp_path, {"hooks": {"stop": [{"command": "true", "tools": ["shell"]}]}}
            )

    def test_unknown_key_and_bad_timeout_refused(self, tmp_path: Path) -> None:
        with pytest.raises(HooksConfigError):
            hooks_from_mapping(tmp_path, {"hooks": {"pre_tool": [{"command": "true"}]}})
        with pytest.raises(HooksConfigError):
            hooks_from_mapping(
                tmp_path, {"hooks": {"stop": [{"command": "true", "timeout_seconds": 0}]}}
            )


# ── What an answer means ─────────────────────────────────────────────────


class TestInterpret:
    def test_exit_two_blocks_with_stderr(self) -> None:
        out = interpret("before_tool", 2, "", "no force pushes\n")
        assert (out.result, out.reason) == ("block", "no force pushes")

    def test_json_block_ask_and_context(self) -> None:
        assert interpret("user_prompt", 0, '{"decision": "block", "reason": "r"}', "").result == (
            "block"
        )
        asked = interpret("before_tool", 0, '{"decision": "ask", "reason": "check"}', "")
        assert (asked.result, asked.reason) == ("ask", "check")
        context = interpret("after_tool", 0, '{"context": "lint: 2 errors"}', "")
        assert (context.result, context.context) == ("context", "lint: 2 errors")

    def test_allow_never_loosens(self) -> None:
        assert interpret("before_tool", 0, '{"decision": "allow"}', "").result == "ok"

    def test_ask_means_nothing_off_before_tool(self) -> None:
        assert interpret("after_tool", 0, '{"decision": "ask"}', "").result == "ok"

    def test_plain_text_is_context_only_for_prompt_and_start(self) -> None:
        assert interpret("user_prompt", 0, "branch: main\n", "").context == "branch: main"
        assert interpret("session_start", 0, "hello", "").result == "context"
        assert interpret("after_tool", 0, "chatter", "").result == "ok"

    def test_other_exit_is_an_error_that_changes_nothing(self) -> None:
        out = interpret("before_tool", 1, '{"decision": "block"}', "boom")
        assert (out.result, out.reason) == ("error", "exited 1")


# ── Running a command ────────────────────────────────────────────────────


class TestRun:
    async def test_payload_on_stdin_and_env(self, tmp_path: Path) -> None:
        command = _script(
            tmp_path,
            "env",
            "import json, os, sys\n"
            "data = json.load(sys.stdin)\n"
            "print(data['prompt'] + ' ' + os.environ['TST_HOOK_EVENT'])\n",
        )
        session = _hooked_session(tmp_path, user_prompt=[HookEntry(command=command)])
        verdict = await session.hooks.run("user_prompt", {"prompt": "hi"})
        assert verdict.context == "hi user_prompt"
        [run] = _runs(session)
        assert (run.hook_id, run.result, run.source) == ("user:user_prompt:0", "context", "user")

    async def test_timeout_kills_and_changes_nothing(self, tmp_path: Path) -> None:
        command = _script(tmp_path, "slow", "import time\ntime.sleep(30)\n")
        session = _hooked_session(
            tmp_path, before_tool=[HookEntry(command=command, timeout_seconds=0.5)]
        )
        started = time.monotonic()
        verdict = await session.hooks.run("before_tool", {}, tool_name="shell")
        assert time.monotonic() - started < 10
        assert verdict.block is None and verdict.ask is None
        assert _runs(session)[0].result == "timeout"

    async def test_tools_glob_selects_hooks(self, tmp_path: Path) -> None:
        command = _answer(tmp_path, "fs", {"decision": "block", "reason": "no"})
        session = _hooked_session(
            tmp_path, before_tool=[HookEntry(command=command, tools=["fs_*"])]
        )
        assert (await session.hooks.run("before_tool", {}, tool_name="shell")).block is None
        assert (await session.hooks.run("before_tool", {}, tool_name="fs_write")).block == "no"

    async def test_block_stops_later_hooks(self, tmp_path: Path) -> None:
        first = _answer(tmp_path, "first", code=2)
        second = _answer(tmp_path, "second")
        session = _hooked_session(
            tmp_path, stop=[HookEntry(command=first), HookEntry(command=second)]
        )
        verdict = await session.hooks.run("stop", {})
        assert verdict.block == "stderr says no"
        assert [r.hook_id for r in _runs(session)] == ["user:stop:0"]


# ── Before and after a tool ──────────────────────────────────────────────


class TestBeforeTool:
    async def test_block_refuses_an_automatic_call(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("secret plan", encoding="utf-8")
        command = _answer(tmp_path, "block", {"decision": "block", "reason": "not today"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        asked: list[str] = []
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c1", "fs_read", {"path": "a.txt"}, session
        )
        assert result.status == "error"
        assert result.error_code == "hook_blocked"
        assert "not today" in result.output and "secret plan" not in result.output
        payload = _stdin(tmp_path, "block")
        # The classifier ran first and its decision is what the hook saw.
        assert payload["tool"]["name"] == "fs_read"
        assert payload["tool"]["decision_class"] == "A"
        assert payload["tool"]["effect"] == "auto"

    async def test_ask_turns_auto_into_an_approval(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        command = _answer(tmp_path, "ask", {"decision": "ask", "reason": "big file"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        asked: list[str] = []
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c1", "fs_read", {"path": "a.txt"}, session
        )
        assert result.status == "success"
        assert asked == ["A hook asked for approval: big file"]

    async def test_ask_refuses_in_an_unattended_run(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        command = _answer(tmp_path, "ask", {"decision": "ask", "reason": "big file"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        asked: list[str] = []
        dispatcher = _dispatcher(tmp_path, asked)
        dispatcher.autonomy_fn = lambda: True
        result = await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"}, session)
        assert result.error_code == "hook_blocked"
        assert asked == []

    async def test_allow_does_not_skip_an_approval(self, tmp_path: Path) -> None:
        command = _answer(tmp_path, "allow", {"decision": "allow"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        asked: list[str] = []
        dispatcher = _dispatcher(tmp_path, asked)
        dispatcher.policy = PolicyConfig(
            rules=[PolicyRule(id="ask-reads", tool="fs_read", effect="ask")]
        )
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"}, session)
        assert len(asked) == 1

    async def test_allow_does_not_lift_a_refusal(self, tmp_path: Path) -> None:
        command = _answer(tmp_path, "allow", {"decision": "allow"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        dispatcher = _dispatcher(tmp_path, [])
        dispatcher.policy = PolicyConfig(
            rules=[PolicyRule(id="no-reads", tool="fs_read", effect="never")]
        )
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        result = await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"}, session)
        assert result.error_code == "policy_denied"
        # Refused before any hook ran: a hook never sees a call it could loosen.
        assert _runs(session) == []

    async def test_classifier_still_required(self, tmp_path: Path) -> None:
        from tstd.tools.dispatch import UnclassifiedToolCall

        command = _answer(tmp_path, "allow", {"decision": "allow"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        dispatcher = _dispatcher(tmp_path, [])
        dispatcher.classifier = None
        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"}, session)
        assert _runs(session) == []

    async def test_after_tool_context_reaches_the_model(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("body", encoding="utf-8")
        command = _answer(tmp_path, "lint", {"context": "lint: clean"})
        session = _hooked_session(tmp_path, after_tool=[HookEntry(command=command)])
        result = await _dispatcher(tmp_path, []).dispatch(
            "c1", "fs_read", {"path": "a.txt"}, session
        )
        assert result.status == "success"
        assert "body" in result.output and "lint: clean" in result.output
        assert _stdin(tmp_path, "lint")["result"]["output"].count("body") == 1


# ── Workspace hooks wait for trust ──────────────────────────────────────


class TestWorkspaceTrust:
    async def test_untrusted_workspace_hooks_do_not_run(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_workspace_hooks(workspace, _answer(tmp_path, "repo", code=2))
        store = TrustStore(tmp_path / "data")
        session = Session(str(workspace))
        session.trust_store = store
        session.config_view = read_view(workspace, store)
        assert session.config_view.state == "pending"
        assert session.hooks.active() == []
        assert (await session.hooks.run("before_tool", {}, tool_name="shell")).block is None
        assert session.hooks.state().workspace_state == "pending"

    async def test_trusted_workspace_hooks_run(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_workspace_hooks(workspace, _answer(tmp_path, "repo", code=2))
        store = TrustStore(tmp_path / "data")
        view = read_view(workspace, store)
        assert view.sha256 is not None
        store.record(workspace, view.sha256, "trusted")
        session = Session(str(workspace))
        session.trust_store = store
        session.config_view = read_view(workspace, store)
        [hook] = session.hooks.active()
        assert hook.id == "workspace:before_tool:0"
        assert (await session.hooks.run("before_tool", {}, tool_name="shell")).block

    def test_invalid_workspace_hooks_are_set_aside_and_said(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        (workspace / ".tst" / "config.yaml").write_text("hooks:\n  nope: []\n", encoding="utf-8")
        session = Session(str(workspace))
        session.config_view = read_view(workspace, None)
        state = session.hooks.state()
        assert state.hooks == []
        assert state.workspace_error is not None and "nope" in state.workspace_error


# ── Audit and inspector ──────────────────────────────────────────────────


class TestAuditAndInspector:
    async def test_every_run_is_audited_redacted(self, tmp_path: Path) -> None:
        secret = "sk-hooktest-" + "a" * 32
        register_secret_value(secret)
        command = _answer(tmp_path, "leak", {"decision": "block", "reason": f"key {secret}"})
        session = _hooked_session(tmp_path, before_tool=[HookEntry(command=command)])
        store = AuditStore(tmp_path / "audit.db")
        writer = AuditWriter(store)
        writer.start()
        writer.attach_session(session)
        await session.hooks.run("before_tool", {}, tool_name="shell", tool_call_id="c9")
        await writer.close()
        rows = (
            AuditStore(tmp_path / "audit.db")
            ._conn.execute(
                "SELECT hook_id, source, event, tool_call_id, result, reason FROM hook_runs"
            )
            .fetchall()
        )
        assert len(rows) == 1
        hook_id, source, event, call_id, result, reason = rows[0]
        assert (hook_id, source, event, call_id, result) == (
            "user:before_tool:0",
            "user",
            "before_tool",
            "c9",
            "block",
        )
        assert secret not in reason
        assert secret not in json.dumps([r.model_dump() for r in _runs(session)])

    async def test_inspector_lists_active_hooks_with_last_result(self, tmp_path: Path) -> None:
        ok = _answer(tmp_path, "ok")
        session = _hooked_session(
            tmp_path,
            session_start=[HookEntry(command=ok, name="greeter")],
            before_tool=[HookEntry(command=ok, tools=["shell"])],
        )
        await session.hooks.run("session_start", {})
        state = session.hooks.state()
        assert [(h.id, h.name) for h in state.hooks] == [
            ("user:session_start:0", "greeter"),
            ("user:before_tool:0", None),
        ]
        assert state.hooks[0].last is not None and state.hooks[0].last.result == "ok"
        assert state.hooks[1].last is None
        # The wire shape round-trips.
        parsed = parse_daemon_event(state.model_dump_json())
        assert isinstance(parsed, HooksState)
        assert parsed.hooks[0].last is not None


# ── The loop: prompt, session start and stop ─────────────────────────────


def _users(mock: MockProvider) -> list[str]:
    return [
        content_as_text(m.content) for call in mock.calls for m in call.messages if m.role == "user"
    ]


class TestLoop:
    async def test_prompt_block_keeps_it_from_the_model(self, tmp_path: Path) -> None:
        command = _answer(tmp_path, "gate", {"decision": "block", "reason": "no secrets"})
        config = make_config()
        config.hooks = HooksConfig(user_prompt=[HookEntry(command=command)])
        session = Session(str(tmp_path))
        mock = MockProvider(sequences={"test-brain": [Script(kind="text", content="hi")]})
        runner = await start_loop(session, TierRouter(lead_turns=80), mock, config)
        try:
            await session.add_user_message("my password is hunter2")
            done = await wait_for_turn(session, 1, 15)
            assert done.error_code == "hook_blocked"
            assert mock.calls == []
            assert session.conversation == []
            assert any(isinstance(e, UserTurn) for e in session.event_log.all_events)
            notes = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
            assert "no secrets" in notes[-1].delta
            assert _stdin(tmp_path, "gate")["prompt"] == "my password is hunter2"
        finally:
            await runner.cancel()

    async def test_start_and_prompt_context_reach_the_model(self, tmp_path: Path) -> None:
        start = _script(tmp_path, "start", "print('repo is on branch main')\n")
        prompt = _script(tmp_path, "prompt", "print('today is Tuesday')\n")
        config = make_config()
        config.hooks = HooksConfig(
            session_start=[HookEntry(command=start)], user_prompt=[HookEntry(command=prompt)]
        )
        session = Session(str(tmp_path))
        mock = MockProvider(sequences={"test-brain": [Script(kind="text", content="ok")]})
        runner = await start_loop(session, TierRouter(lead_turns=80), mock, config)
        try:
            await session.add_user_message("what branch?")
            await wait_for_turn(session, 1, 15)
            [sent] = _users(mock)
            assert sent.startswith("what branch?")
            assert "repo is on branch main" in sent and "today is Tuesday" in sent
            assert [r.event for r in _runs(session)] == ["session_start", "user_prompt"]
        finally:
            await runner.cancel()

    async def test_stop_block_sends_the_model_back_a_bounded_number_of_times(
        self, tmp_path: Path
    ) -> None:
        command = _answer(tmp_path, "stop", {"decision": "block", "reason": "run the tests"})
        config = make_config()
        config.hooks = HooksConfig(stop=[HookEntry(command=command)])
        session = Session(str(tmp_path))
        mock = MockProvider(
            sequences={"test-brain": [Script(kind="text", content=f"done {i}") for i in range(6)]}
        )
        runner = await start_loop(session, TierRouter(lead_turns=80), mock, config)
        try:
            await session.add_user_message("fix it")
            done = await wait_for_turn(session, 1, 30)
            assert not done.failed
            # One first answer plus three sends back, then the turn ends.
            assert len(mock.calls) == 4
            assert sum("run the tests" in u for u in _users(mock)[-1:]) == 1
            assert len([r for r in _runs(session) if r.event == "stop"]) == 4
        finally:
            await runner.cancel()


async def test_hook_runner_survives_without_hooks(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    assert isinstance(session.hooks, HookRunner)
    verdict = await session.hooks.run("before_tool", {}, tool_name="shell")
    assert verdict.block is None and verdict.contexts == []
    assert _runs(session) == []
    assert [e for e in session.event_log.all_events if isinstance(e, TurnComplete)] == []
