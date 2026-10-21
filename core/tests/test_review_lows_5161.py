"""TD-5161: low-severity review notes from waves 7-10.

Each test pins one note that was real on main: it fails on the old code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_backup_endpoints import BACKUP_TIER, _config
from tests.test_loop import wait_for_turn
from tests.test_steer_turn import _brain, _HeldTool, _start, _tool_then_text
from tstd.autonomy.dod import DodItemResult
from tstd.cli_turn import _TurnReader
from tstd.config import load_config
from tstd.config_write import save_tier_slug
from tstd.fallback import TurnBackups
from tstd.local_worker import tier_state_event
from tstd.protocol import (
    ApprovalRequest,
    Question,
    QuestionOption,
    QuestionRequest,
    SessionGoal,
)
from tstd.provider import content_as_text
from tstd.router import TierRouter
from tstd.scheduler.park import pending_tool_summary, waiting_line
from tstd.session import Session
from tstd.stop_check import feedback_text
from tstd.tools.consult import CONSULT_TOOL


def test_consult_tells_a_brain_its_default_is_the_validator() -> None:
    """Note 9: a brain told "brain (the default)" was steered into a refusal."""
    to = CONSULT_TOOL.parameters["properties"]["to"]["description"]
    for text in (CONSULT_TOOL.description, to):
        assert "brain (the default)" not in text and "brain (default)" not in text
        assert "validator when you are the brain" in text


_PRICES = """input_price: 0
{i}output_price: 0
{i}cache_read_price: 0
{i}context_window: 32768
{i}max_output_tokens: 1000"""


def test_a_slug_write_never_splices_into_a_backup(tmp_path: Path) -> None:
    """Note 10: a commented `slug:` inside a `fallbacks` entry was the placeholder."""
    tier = "      base_url: http://127.0.0.1:8080/v1\n      " + _PRICES.format(i="      ")
    text = f"""active_preset: p
presets:
  p:
    brain:
      fallbacks:
        - slug: vendor/backup
          # slug: vendor/older-backup
          base_url: http://127.0.0.1:8081/v1
          {_PRICES.format(i="          ")}
{tier}
    worker:
      slug: m
{tier}
    validator:
      slug: m
{tier}
"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).tier("brain").slug in (None, "")
    save_tier_slug("p", "brain", "new-model", path)
    brain = load_config(path).tier("brain")
    assert brain.slug == "new-model"
    assert [b.slug for b in brain.fallbacks] == ["vendor/backup"]
    assert "# slug: vendor/older-backup" in path.read_text(encoding="utf-8")


def test_a_mid_turn_tier_state_keeps_naming_the_backup() -> None:
    """Note 11: set_plan/set_tier mid-turn reset the pill to the dead endpoint."""
    config = _config(BACKUP_TIER)
    session = Session("/tmp/ws")
    session.router = TierRouter()
    backups = TurnBackups()
    chain = backups.chain_for(config, "brain", config.tier("brain"))
    chain.advance()
    session.turn_backups = backups
    session._open_turns = 1
    during = tier_state_event(session, config)
    assert during.fallbacks == {"brain": 1}
    assert during.model_slugs["brain"] == "backup-model"
    assert during.hosts["brain"] == "backup.local"
    session._open_turns = 0  # between turns the next one starts on the own model
    after = tier_state_event(session, config)
    assert after.fallbacks == {} and after.model_slugs["brain"] == "own-model"


async def test_a_run_parked_on_a_question_says_so() -> None:
    """Note 16: the channel called a question an approval, with an older card's summary."""
    session = Session("/tmp/ws")
    await session.event_log.add(
        ApprovalRequest(
            session_id=session.id,
            tool_call_id="c1",
            tool_name="shell",
            arguments={},
            decision_class="B",
            summary="run `make deploy`",
            reason="",
            seq=1,
        )
    )
    options = [QuestionOption(label="Postgres"), QuestionOption(label="SQLite")]
    await session.event_log.add(
        QuestionRequest(
            session_id=session.id,
            tool_call_id="c2",
            questions=[Question(question="Which database?", options=options)],
            seq=1,
        )
    )
    summary = pending_tool_summary(session)
    assert "make deploy" not in summary
    assert waiting_line(None, None, summary) == (
        "This job is waiting for your answer to: Which database?"
    )
    assert waiting_line(None, None, "run `ls`") == "This job is waiting for your approval: run `ls`"


async def test_tst_run_returns_when_the_turn_pauses() -> None:
    """Note 19: a paused turn (the progress guard) left `tst run` waiting out its budget."""
    reader = _TurnReader(session_id="s", output="json", ours=True, content=None)
    paused = {
        "type": "session_state",
        "session_id": "s",
        "state": "paused",
        "reason": "progress guard: the same tool batch three times in a row",
    }
    outcome = await reader.feed(None, paused)
    assert outcome is not None
    assert (outcome.status, outcome.exit_code) == ("approval_needed", 4)
    assert "progress guard" in (outcome.error_message or "")
    running = {"type": "session_state", "session_id": "s", "state": "running"}
    assert (
        await _TurnReader(session_id="s", output="json", ours=True, content=None).feed(
            None, running
        )
        is None
    )


async def test_a_goal_clear_sent_mid_turn_clears_the_goal() -> None:
    """Note 24: a steered `/goal clear` went to the model as text; the goal stayed."""
    session = Session("/tmp/ws")
    session.goal = "ship it"
    held = _HeldTool()
    runner, mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("/goal clear", steer=True)
        held.release.set()
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
    assert session.goal is None
    goals = [(e.goal, e.change) for e in session.event_log.all_events if isinstance(e, SessionGoal)]
    assert goals == [(None, "cleared")]
    _first, second = _brain(mock)
    texts = [content_as_text(m.content) for m in second.messages if m.role == "user"]
    assert "/goal clear" not in texts
    assert "Goal cleared: ship it" in texts


def test_a_command_goal_gets_its_output_back() -> None:
    """Note 25: a `$` goal ran as a command, but the feedback dropped its output."""
    failed = DodItemResult("$ false", False, "exit 1\nboom", "shell")
    text = feedback_text([(failed, "goal")], 1, 3)
    assert "Goal not met yet: `$ false` failed" in text
    assert "boom" in text
    judged = DodItemResult("the parser handles leap days", False, "RED", "worker")
    assert "- Goal not met yet: the parser handles leap days" in feedback_text(
        [(judged, "goal")], 1, 3
    )


async def test_a_backup_offers_tools_for_its_own_window(tmp_path: Path) -> None:
    """Note 26: the backup got the tool list sized for the primary's window.

    The MCP schemas fit 10% of the primary's 100k window, so the primary
    sends them; the 32k backup must defer them behind ``tool_search``.
    """
    import json

    import httpx

    from tests.test_backup_endpoints import BACKUP, PRIMARY, _answer, _Endpoints, _refused, _Run
    from tests.test_dispatch import attach_auto_approver
    from tests.test_mcp_tool_search import _mcp_tokens, _mcp_tool, _registry
    from tstd.tools import ToolDispatcher
    from tstd.tools.handlers import register_builtin_handlers
    from tstd.tools.tool_search import TOOL_SEARCH_NAME

    registry = _registry(
        [_mcp_tool("big", f"tool_{n}", "Does a thing. " * 120, arg="An argument") for n in range(8)]
    )
    assert 32_768 // 10 < _mcp_tokens(registry) < 100_000 // 10
    dispatcher = ToolDispatcher(registry)
    register_builtin_handlers(dispatcher)
    sent: list[tuple[str, list[str]]] = []

    class _Recording(_Endpoints):
        def handler(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            names = [t["function"]["name"] for t in body.get("tools") or []]
            sent.append((request.url.host, names))
            return super().handler(request)

    endpoints = _Recording({PRIMARY: _refused, BACKUP: lambda _: _answer("From the backup.")})
    run = _Run(
        tmp_path,
        endpoints,
        _config(BACKUP_TIER, retries=0),
        registry=registry,
        dispatcher=attach_auto_approver(dispatcher),
    )
    try:
        turn = await run.send("fix it")
    finally:
        await run.close()
    assert not turn.failed
    primary = next(names for host, names in sent if host == "primary.local")
    backup = next(names for host, names in sent if host == "backup.local")
    assert "big__tool_0" in primary
    assert TOOL_SEARCH_NAME in backup and "big__tool_0" not in backup


async def test_a_select_loads_every_name_it_can_and_says_what_it_left() -> None:
    """Note 27: select: dropped names past max_results (5) without a word."""
    from tests.test_mcp_tool_search import _mcp_tool, _registry
    from tstd.tools.tool_search import MAX_RESULTS_CAP, tool_search

    registry = _registry(
        [_mcp_tool("s", f"t{n}", f"Tool {n}.") for n in range(MAX_RESULTS_CAP + 2)]
    )
    session = Session("/tmp/ws")
    names = ",".join(f"t{n}" for n in range(MAX_RESULTS_CAP + 2))
    text = await tool_search(registry=registry, session=session, query=f"select:{names}")
    assert session.loaded_mcp_tools == [f"s__t{n}" for n in range(MAX_RESULTS_CAP)]
    assert f"s__t{MAX_RESULTS_CAP}, s__t{MAX_RESULTS_CAP + 1}" in text
    assert "Select them in another call." in text


def test_a_loaded_tools_file_that_is_not_utf8_reads_as_none(tmp_path: Path) -> None:
    """Note 28: a non-UTF-8 file raised UnicodeDecodeError into the restore."""
    from tstd.session_persist import SessionPersist

    persist = SessionPersist(tmp_path)
    folder = persist.dir_for("s1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mcp_tools.json").write_bytes(b'["\xff\xfe"]')
    assert persist.load_loaded_tools("s1") == []


async def test_a_call_parked_before_plan_mode_is_refused_when_released(tmp_path: Path) -> None:
    """Note 30: plan mode turned on while a write waited; skip-all ran it."""
    from typing import Any

    from tstd.autonomy.classifier import Boundary, DecisionClassifier
    from tstd.autonomy.worker import AmbiguousClassifier
    from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule
    from tstd.tools import ToolDispatcher, create_registry
    from tstd.tools.boundary import PathGuard
    from tstd.tools.handlers import register_builtin_handlers

    planning = False

    async def approve_after_plan_mode_starts(*_args: Any) -> ApprovalOutcome:
        nonlocal planning
        planning = True  # the person turned plan mode on, then skip-all released it
        return ApprovalOutcome(approved=True)

    async def never_asked(*_args: Any) -> Any:
        raise AssertionError("no classifier question here")

    boundary = Boundary(workspace_root=tmp_path, plan_mode=lambda: planning)
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(DecisionClassifier(boundary), call_worker=never_asked),
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")]),
        approval_handler=approve_after_plan_mode_starts,
        workspace=tmp_path,
    )
    register_builtin_handlers(dispatcher)
    result = await dispatcher.dispatch(
        "tc-1", "fs_write", {"path": str(tmp_path / "out.txt"), "content": "hi"}
    )
    assert (result.status, result.error_code) == ("error", "plan_mode")
    assert not (tmp_path / "out.txt").exists()


async def test_a_message_while_the_worktree_closes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Note 34: a message during a slow close started a turn in a folder being deleted."""
    import json

    import tstd.daemon as daemon_module
    from tests.test_artifacts import _open, _shutdown
    from tests.test_worktree import make_repo
    from tests.test_worktree_daemon import _send
    from tstd.daemon import Daemon

    gate = asyncio.Event()
    real_close = daemon_module.close_worktree

    async def slow_close(*args: object, **kwargs: object) -> str | None:
        await gate.wait()
        return await real_close(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(daemon_module, "close_worktree", slow_close)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, make_repo(tmp_path / "repo"))
    try:
        closing = asyncio.create_task(_send(daemon, {"type": "close_worktree", "session_id": sid}))
        await asyncio.sleep(0.05)
        message = {"type": "user_message", "session_id": sid, "content": "go"}
        refused = json.loads(await daemon._handle_message(json.dumps(message), None) or "{}")
        assert refused.get("code") == "session_busy"
        assert "closing its worktree" in refused.get("message", "")
        gate.set()
        assert (await closing)["type"] == "session_list"
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.folder_busy is None
    finally:
        await _shutdown(daemon, sid)


async def test_a_restore_holds_new_messages_while_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Note 38: a turn could start while a restore rewrote the folder."""
    import tstd.rewind_handlers as rewind_handlers
    from tstd.protocol import RestoreFiles, UserTurn

    session = Session(str(tmp_path))
    await session.event_log.add(UserTurn(session_id=session.id, content="go", turn_id="t1", seq=1))
    seen: list[str | None] = []

    async def apply(*_args: object, **_kwargs: object) -> object:
        seen.append(session.folder_busy)
        raise rewind_handlers.RewindError("rewind_failed", "stopped")

    monkeypatch.setattr(rewind_handlers, "apply_rewind", apply)
    msg = RestoreFiles(session_id=session.id, turn_id="t1", plan_id="p1")
    assert await rewind_handlers.restore_files(session, msg) is not None
    assert seen == ["restoring files"]
    assert session.folder_busy is None


@pytest.mark.parametrize(
    "module", ["tstd.worktree_session", "tstd.context.workers", "tstd.tools.delegate_named"]
)
def test_a_module_imports_on_its_own(module: str) -> None:
    """Notes 35 and 69: these failed unless another module was imported first."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-400:]


def test_a_relative_worktree_folder_is_refused() -> None:
    """Note 36: Python made it under the daemon's cwd, git under the repository."""
    from pydantic import ValidationError

    from tstd.config import WorktreeConfig

    with pytest.raises(ValidationError, match="absolute path"):
        WorktreeConfig(folder="trees")
    assert WorktreeConfig(folder="~/trees").folder == "~/trees"
    assert WorktreeConfig(folder="").folder == ""


async def test_a_session_whose_worktree_is_setting_up_is_not_deleted(tmp_path: Path) -> None:
    """Note 37: a delete mid-setup left the worktree and its branch for good."""
    from dataclasses import replace

    from tests.test_artifacts import _open, _shutdown
    from tests.test_worktree import make_repo
    from tests.test_worktree_daemon import _send
    from tstd.daemon import Daemon

    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, make_repo(tmp_path / "repo"))
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.worktree is not None
        sess.worktree = replace(sess.worktree, setup="running")
        refused = await _send(daemon, {"type": "delete_session", "session_id": sid})
        assert refused.get("code") == "worktree_busy"
        assert daemon._session_store.get(sid) is not None
        assert Path(sess.worktree.path).is_dir()
    finally:
        await _shutdown(daemon, sid)
