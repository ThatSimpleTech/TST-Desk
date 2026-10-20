"""The static read-only command list for shell: running and recording (TD-5103).

A listed command runs through the dispatcher without an approval card,
policy rules still decide first, the audit database names the list
entries that let a call run, and a listed git command runs with git's
command hooks pinned off.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

import tstd.shell_read_only as shell_read_only
from tests.test_loop import make_config, mock_factory, wait_for_turn
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.config import ModelConfig, ShellReadOnlyConfig
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule, read_guards
from tstd.protocol import ApprovalRequest
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.shell_read_only import READ_ONLY_ENTRIES, run_env
from tstd.tools import create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.shell import run_shell

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the list is POSIX only")

ALL = frozenset(READ_ONLY_ENTRIES)


def _git_version() -> tuple[int, int]:
    git = shutil.which("git")
    if git is None:
        return (0, 0)
    out = subprocess.run([git, "--version"], capture_output=True, text=True, check=False).stdout
    found = re.search(r"(\d+)\.(\d+)", out)
    return (int(found[1]), int(found[2])) if found else (0, 0)


needs_git_240 = pytest.mark.skipif(_git_version() < (2, 40), reason="needs git 2.40+")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace, and a PATH of absolute entries only."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("hello\nworld\n")
    search = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    monkeypatch.setenv("PATH", os.pathsep.join(search))
    return root


def _config(enabled: bool = True) -> ModelConfig:
    config = make_config()
    shell = config.shell.model_copy(update={"read_only": ShellReadOnlyConfig(enabled=enabled)})
    return config.model_copy(update={"shell": shell})


class _Gate:
    """A dispatcher over the list, recording every approval it is asked for."""

    def __init__(self, root: Path, policy: PolicyConfig | None = None) -> None:
        self.asked: list[str] = []
        self.worker: list[str] = []
        gate_policy = policy if policy is not None else PolicyConfig()
        boundary = Boundary(
            workspace_root=root,
            shell_read_only=ALL,
            read_guards=lambda: read_guards(gate_policy),
        )

        async def worker(prompt: str) -> str:
            self.worker.append(prompt)
            return "B"

        self.dispatcher = ToolDispatcher(
            create_registry(),
            classifier=AmbiguousClassifier(static=DecisionClassifier(boundary), call_worker=worker),
            path_guard=PathGuard(boundary),
            policy=gate_policy,
            workspace=root,
        )
        register_builtin_handlers(self.dispatcher)

        async def approve(_id, _tool, arguments, _cls, _summary, _reason) -> ApprovalOutcome:
            self.asked.append(str(arguments.get("command")))
            return ApprovalOutcome(True)

        self.dispatcher.approval_handler = approve
        self.session = Session(str(root))
        self.session.config = _config()

    async def shell(self, command: str):
        return await self.dispatcher.dispatch("tc", "shell", {"command": command}, self.session)

    async def call(self, name: str, arguments: dict[str, object]):
        return await self.dispatcher.dispatch("tc", name, arguments, self.session)


# ── AC1: a listed command runs without asking ───────────────────────────


async def test_a_listed_command_runs_without_asking(ws: Path) -> None:
    gate = _Gate(ws)
    result = await gate.shell("cat a.txt | head -n 1")
    assert result.status == "success"
    assert "hello" in result.output
    assert gate.asked == []
    assert gate.worker == []
    assert result.allowed_by == ("cat", "head")


async def test_anything_else_still_asks(ws: Path) -> None:
    gate = _Gate(ws)
    result = await gate.shell("cat a.txt > b.txt")
    assert gate.asked == ["cat a.txt > b.txt"]
    assert result.allowed_by == ()


async def test_a_policy_rule_still_decides_first(ws: Path) -> None:
    def rule(args: str, effect: Literal["auto", "ask", "never"]) -> PolicyConfig:
        return PolicyConfig(rules=[PolicyRule(tool="shell", args=args, effect=effect)])

    ask = _Gate(ws, rule("cat *", "ask"))
    assert (await ask.shell("cat a.txt")).allowed_by == ()
    assert ask.asked == ["cat a.txt"]

    never = _Gate(ws, rule("cat *", "never"))
    refused = await never.shell("cat a.txt")
    assert refused.error_code == "policy_denied"
    assert never.asked == []

    auto = _Gate(ws, rule("cat a.txt", "auto"))
    ran = await auto.shell("cat a.txt")
    assert ran.status == "success"
    assert ran.allowed_by == ()  # the user's rule let it run, not the list


async def test_the_documented_file_guard_holds_for_the_list(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/configuration.md's TD-5141 recipe: a never rule for fs_read and
    fs_grep on `*.env*`. The listed commands that would print .env ask, as
    every shell call did before the list (review of PR #31)."""
    (ws / ".env").write_text("API_KEY=1\n")
    (ws / ".git").mkdir()
    monkeypatch.setattr(shell_read_only, "git_ready", lambda: True)
    commands = ("cat .env", "grep -r API_KEY .", "git show HEAD:.env")
    free = _Gate(ws)
    for command in commands:
        assert (await free.shell(command)).allowed_by != (), command
    assert free.asked == []

    rules = [
        PolicyRule(tool="fs_read", args="*.env*", effect="never"),
        PolicyRule(tool="fs_grep", args="*.env*", effect="never"),
    ]
    gate = _Gate(ws, PolicyConfig(rules=rules))
    refused = await gate.call("fs_read", {"path": ".env"})
    assert refused.error_code == "policy_denied"
    for command in commands:
        assert (await gate.shell(command)).allowed_by == (), command
    assert gate.asked == list(commands)
    ran = await gate.shell("cat a.txt")
    assert ran.status == "success"
    assert ran.allowed_by == ("cat",)
    assert gate.asked == list(commands)


# ── AC4: the audit database names the entries ──────────────────────────


async def test_the_audit_names_the_entries(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    for call_id, command, allowed in (
        ("tc1", "git log | head", ["git log", "head"]),
        ("tc2", "make", []),
    ):
        await session.event_log.add(
            ToolCallEvent(
                session_id=session.id,
                tool_call_id=call_id,
                name="shell",
                arguments={"command": command},
                decision_class="A" if allowed else "B",
                seq=1,
            )
        )
        await session.event_log.add(
            ToolResultEvent(
                session_id=session.id,
                tool_call_id=call_id,
                status="success",
                output="ok",
                allowed_by=allowed,
                seq=1,
            )
        )
    await writer._queue.join()
    rows = store._conn.execute(
        "SELECT t.tool_call_id, a.allowed_by FROM tool_calls t"
        " LEFT JOIN allowed_calls a ON a.tool_call = t.id ORDER BY t.id"
    ).fetchall()
    assert rows == [("tc1", "git log, head"), ("tc2", None)]
    await writer.close()
    store.close()


async def test_a_loop_turn_runs_and_records_the_entry(ws: Path, tmp_path: Path) -> None:
    """The real wiring: the loop builds the classifier from the session's config."""
    store = AuditStore(tmp_path / "audit.db")
    session = Session(str(ws))
    session.config = _config()
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    registry = create_registry()
    dispatcher = ToolDispatcher(registry)
    register_builtin_handlers(dispatcher)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="shell",
                    tool_arguments='{"command": "cat a.txt | head -n 1"}',
                ),
                Script(kind="stream", content="Done"),
            ]
        }
    )
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(lead_turns=3),
            mock_factory(mock),
            session.config or make_config(),
            tool_registry=registry,
            tool_dispatcher=dispatcher,
            audit_sink=writer,
        ),
    )
    await runner.start()
    await session.add_user_message("show the first line")
    await wait_for_turn(session, 1, 10.0)
    events = session.event_log.all_events
    assert not [e for e in events if isinstance(e, ApprovalRequest)]
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [c.decision_class for c in calls] == ["A"]
    assert [r.allowed_by for r in results] == [["cat", "head"]]
    assert "hello" in results[0].output
    await writer._queue.join()
    row = store._conn.execute(
        "SELECT t.name, a.allowed_by FROM tool_calls t JOIN allowed_calls a ON a.tool_call = t.id"
    ).fetchone()
    assert row == ("shell", "cat, head")
    await runner.cancel()
    await writer.close()
    store.close()


# ── git runs with its command hooks pinned off ──────────────────────────


def test_run_env_pins_only_listed_git(tmp_path: Path) -> None:
    pins = run_env("git status", {}, tmp_path)
    assert pins["GIT_ATTR_SOURCE"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert pins["GIT_OPTIONAL_LOCKS"] == "0"
    keys = {pins[f"GIT_CONFIG_KEY_{i}"]: pins[f"GIT_CONFIG_VALUE_{i}"] for i in range(4)}
    assert keys == {
        "core.fsmonitor": "false",
        "core.hooksPath": os.devnull,
        "safe.bareRepository": "explicit",
        "core.attributesFile": os.devnull,
    }
    assert pins["GIT_CONFIG_COUNT"] == "4"
    shifted = run_env("ls | git log", {"GIT_CONFIG_COUNT": "2"}, tmp_path)
    assert shifted["GIT_CONFIG_COUNT"] == "6"
    assert shifted["GIT_CONFIG_KEY_2"] == "core.fsmonitor"
    assert run_env("ls", {}, tmp_path) == {}
    assert run_env("git commit -m x", {}, tmp_path) == {}
    assert run_env("git status > x", {}, tmp_path) == {}


def test_run_env_pins_whatever_the_configured_list_says(tmp_path: Path) -> None:
    """TD-5160: the classifier's copy of the list can be older than the
    session's, so the pins never depend on it: a git command it still
    auto-runs from a stale list runs pinned."""
    assert run_env("git status", {}, tmp_path)["GIT_ATTR_SOURCE"]
    assert run_env("git diff", {}, tmp_path)["GIT_CONFIG_COUNT"] == "4"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


@needs_git_240
def _bump_mtime(path: Path) -> None:
    later = path.stat().st_mtime + 10
    os.utime(path, (later, later))


async def test_listed_git_runs_no_filter_textconv_or_hook(ws: Path, tmp_path: Path) -> None:
    """An in-tree ``.gitattributes`` (a Class A write) routes ``a.txt`` through
    a filter the user's own config names, the user's attributes file, kept
    in the workspace, routes it through a textconv driver, and an in-tree
    hooks folder holds a ``post-index-change`` hook. Run from the list, git
    runs none of them; with the list off the same commands run all three."""
    marks = tmp_path / "marks"
    marks.mkdir()
    _git(ws, "init", "-q")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("commit.gpgSign", "false"),
        ("filter.mark.clean", _script(marks / "clean.sh", f"touch {marks}/filter; cat")),
        ("diff.mark.textconv", _script(marks / "tc.sh", f'touch {marks}/textconv; cat "$1"')),
        ("core.hooksPath", ".githooks"),
        ("core.attributesFile", str(ws / "user.attributes")),
    ):
        _git(ws, "config", key, value)
    (ws / ".githooks").mkdir()
    _script(ws / ".githooks" / "post-index-change", f"touch {marks}/hook")
    (ws / ".gitattributes").write_text("*.txt filter=mark\n")
    (ws / "user.attributes").write_text("*.txt diff=mark\n")
    (ws / "same.dat").write_text("unchanged\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-qm", "base")
    for mark in ("filter", "textconv", "hook"):  # the commit itself ran some
        (marks / mark).unlink(missing_ok=True)
    (ws / "a.txt").write_text("hello\nthere\n")  # same size, new bytes
    assert sorted(p.name for p in marks.iterdir()) == ["clean.sh", "tc.sh"]

    _bump_mtime(ws / "same.dat")
    await shell_read_only.probe_git()
    session = Session(str(ws))
    session.config = _config()
    status = await run_shell(session, "git status --short")
    diff = await run_shell(session, "git diff")
    assert "a.txt" in status
    assert "+there" in diff
    assert sorted(p.name for p in marks.iterdir()) == ["clean.sh", "tc.sh"]

    # The list off still pins a command it would read (TD-5160).
    _bump_mtime(ws / "same.dat")
    session.config = _config(enabled=False)
    await run_shell(session, "git status --short")
    await run_shell(session, "git diff")
    assert sorted(p.name for p in marks.iterdir()) == ["clean.sh", "tc.sh"]

    # A newer timestamp on unchanged content makes any git refresh and
    # rewrite the index, which is what fires post-index-change; without it
    # older git (CI's) may leave the index alone and the control proves nothing.
    # The control: `-c` is a form the list never reads, so nothing is pinned.
    _bump_mtime(ws / "same.dat")
    await run_shell(session, "git -c color.ui=false status --short")
    await run_shell(session, "git -c color.ui=false diff")
    ran = {p.name for p in marks.iterdir()} - {"clean.sh", "tc.sh"}
    assert ran == {"filter", "textconv", "hook"}


# ── Review notes (TD-5160) ──────────────────────────────────────────────


def _committed_repo(ws: Path, *init: str) -> None:
    _git(ws, "init", "-q", *init)
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("commit.gpgSign", "false"),
    ):
        _git(ws, "config", key, value)
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-qm", "base")
    (ws / "a.txt").write_text("hello\nworld\nagain\n")


@needs_git_240
async def test_a_program_the_git_config_names_makes_its_entry_ask(ws: Path, tmp_path: Path) -> None:
    """The user's config includes an in-tree file, which a Class A write can
    fill: ``diff.external`` there would run on a plain ``git diff``, and
    signature checks would run the gpg program. Those entries ask."""
    await shell_read_only.probe_git()
    _committed_repo(ws)
    marks = tmp_path / "marks"
    marks.mkdir()
    tool = _script(ws / "d.sh", f"touch {marks}/ran")
    _git(ws, "config", "include.path", "../devtools.cfg")
    gate = _Gate(ws)

    (ws / "devtools.cfg").write_text(f"[diff]\n\texternal = {tool}\n")
    for command in ("git status --short", "git log -1 --oneline", "git show --stat"):
        await gate.shell(command)
    assert gate.asked == []
    assert list(marks.iterdir()) == []
    await gate.shell("git diff")
    assert gate.asked == ["git diff"]

    gate.asked.clear()
    for cfg in ("[log]\n\tshowSignature = true\n", "[pretty]\n\tsig = %h %G?\n"):
        (ws / "devtools.cfg").write_text(cfg)
        for command in ("git log -1", "git show --stat"):
            await gate.shell(command)
        assert gate.asked == ["git log -1", "git show --stat"], cfg
        gate.asked.clear()

    (ws / "devtools.cfg").write_text("")
    for command in (
        "git log -1 --format=%G?",
        "git show --pretty=format:%GS",
        "git branch --format=%(signature)",
    ):
        await gate.shell(command)
    await gate.shell("git log -1 --format=%h")
    assert gate.asked == [
        "git log -1 --format=%G?",
        "git show --pretty=format:%GS",
        "git branch --format=%(signature)",
    ]


@needs_git_240
async def test_a_sha256_repository_diffs_from_the_list(ws: Path) -> None:
    """The SHA-1 empty tree is no name in a SHA-256 repository, so every
    pinned git diff, log -p or show failed there, approved or not."""
    await shell_read_only.probe_git()
    _committed_repo(ws, "--object-format=sha256")
    gate = _Gate(ws)
    diff = await gate.shell("git diff")
    show = await gate.shell("git show --stat")
    assert gate.asked == []
    assert "+again" in diff.output, diff.output
    assert "a.txt" in show.output, show.output
