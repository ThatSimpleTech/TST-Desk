"""TD-5161: low-severity review notes from waves 7-10.

Each test pins one note that was real on main: it fails on the old code.
"""

from __future__ import annotations

import asyncio
import os
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
        assert await asyncio.to_thread(Path(sess.worktree.path).is_dir)
    finally:
        await _shutdown(daemon, sid)


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="needs FIFOs")
async def test_a_file_swapped_for_a_fifo_never_blocks_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Note 49: the stat said regular file, then the read opened a FIFO and blocked."""
    import contextlib
    import os

    from tstd.tools.diff import snapshot_text

    regular = tmp_path / "regular.txt"
    regular.write_text("hi", encoding="utf-8")
    fifo = tmp_path / "swapped"
    os.mkfifo(fifo)
    real_stat = Path.stat
    # What a stat saw just before the swap: a regular file.
    monkeypatch.setattr(
        Path, "stat", lambda self, **kw: real_stat(regular if self == fifo else self, **kw)
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(snapshot_text, fifo), 2) is None
    finally:
        # Unblock a reader the old code left waiting on the FIFO.
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))


async def test_a_cancelled_summary_ahead_falls_back_instead_of_raising() -> None:
    """Note 51: a summary written ahead and cancelled raised CancelledError."""
    from tstd.compaction_model import CompactionSummarizer, _Ahead, _fingerprint
    from tstd.config import CompactionConfig, TierConfig
    from tstd.provider import ChatMessage

    async def never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no new summary call")

    worker = TierConfig(
        slug="w",
        base_url="http://127.0.0.1:9/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=32_768,
        max_output_tokens=1_000,
    )
    summarizer = CompactionSummarizer(
        CompactionConfig(summary="model"),
        worker,
        never,
        lambda: None,  # type: ignore[arg-type,return-value]
    )
    dropped = [ChatMessage(role="user", content="earlier")]
    key = _fingerprint(dropped, "")
    task: asyncio.Task[str | None] = asyncio.create_task(asyncio.sleep(10))  # type: ignore[arg-type]
    summarizer._ahead[key] = _Ahead(key, task)
    asyncio.get_running_loop().call_later(0.01, task.cancel)
    assert await summarizer.summarize(dropped) is None


def test_the_page_images_schema_does_not_promise_five() -> None:
    """Note 56: the schema said "up to 5"; the default cap is 1."""
    from tstd.config import ComputerUseConfig
    from tstd.tools import create_registry

    fs_read = create_registry().get("fs_read")
    assert fs_read is not None
    text = fs_read.parameters["properties"]["page_images"]["description"]
    assert "up to 5" not in text
    assert ComputerUseConfig().max_prompt_images == 1


def test_an_xlsx_date_reaches_the_model_as_a_date() -> None:
    """Note 61: dates went out as Excel serial numbers (45583, not 2024-10-18)."""
    import io
    import zipfile

    from tstd.attachment_extract import extract_document

    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    styles = (
        f'<styleSheet {ns}><numFmts count="1">'
        '<numFmt numFmtId="164" formatCode="dd/mm/yyyy hh:mm"/></numFmts>'
        '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/>'
        '<xf numFmtId="164"/><xf numFmtId="2"/></cellXfs></styleSheet>'
    )
    cells = (
        '<c r="A1" s="1"><v>45583</v></c><c r="B1" s="2"><v>45583.5</v></c>'
        '<c r="C1" s="3"><v>45583</v></c><c r="D1"><v>7</v></c>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            f'<workbook {ns}><sheets><sheet name="S" sheetId="1"/></sheets></workbook>',
        )
        zf.writestr("xl/styles.xml", styles)
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet {ns}><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
        )
    text = extract_document("d.xlsx", "xlsx", out.getvalue(), max_text_bytes=10_000).text
    assert "2024-10-18\t2024-10-18 12:00:00\t45583\t7" in text


def test_the_1904_epoch_and_a_bare_time() -> None:
    from tstd.attachment_xlsx_dates import as_date, is_date_format

    assert as_date("0", epoch_1904=True) == "1904-01-01"
    assert as_date("0.25") == "06:00:00"
    assert as_date("nope") is None
    assert is_date_format("[$-409]mmm d, yyyy") and not is_date_format('0.00 "days"')
    assert not is_date_format("[Red]0.00")


def test_the_window_knows_the_output_caps_marker() -> None:
    """Note 64: the window strips this marker from a cut diff; keep them the same."""
    import json

    from tstd.tools.results import _TRUNCATION_MARKER

    ts = (Path(__file__).parents[2] / "ui/src/lib/review-comments.ts").read_text(encoding="utf-8")
    assert f"TRUNCATION_MARKER = {json.dumps(_TRUNCATION_MARKER, ensure_ascii=False)};" in ts


def test_a_worker_definition_write_is_not_sent_to_propose_steering_edit(tmp_path: Path) -> None:
    """Note 71: the refusal pointed at a tool that refuses agents files too."""
    from tstd.autonomy.classifier import Boundary
    from tstd.tools.boundary import PathGuard, RefusalError

    guard = PathGuard(Boundary(workspace_root=tmp_path))
    with pytest.raises(RefusalError) as agents:
        guard.check_write(tmp_path / ".tst" / "agents" / "x.md")
    assert "propose_steering_edit" not in agents.value.reason
    with pytest.raises(RefusalError) as rules:
        guard.check_write(tmp_path / "AGENTS.md")
    assert "propose_steering_edit" in rules.value.reason


def test_a_new_worker_definition_counts_as_a_worktree_change() -> None:
    """Note 70: .tst/agents was missing from the shared folders, unlike .tst/commands."""
    from tstd.worktree import _own_untracked

    assert not _own_untracked("??", ".tst/agents/reviewer.md")
    assert not _own_untracked("??", ".tst/commands/ship.md")
    assert _own_untracked("??", ".tst/memory/notes.md")


async def test_a_childs_hooks_run_in_its_own_worktree(tmp_path: Path) -> None:
    """Note 73: a worktree child's after_tool lint ran in the parent's tree."""
    import sys

    from tstd.hooks_config import HookEntry, HooksConfig

    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    seen = tmp_path / "cwd.txt"
    script = tmp_path / "where.py"
    script.write_text(
        f"import os, pathlib\npathlib.Path({str(seen)!r}).write_text(os.getcwd() + '|' + "
        "os.environ['TST_WORKSPACE'])\n",
        encoding="utf-8",
    )
    parent = Session(str(parent_root))
    parent.hooks.user = HooksConfig(
        after_tool=[HookEntry(command=f'"{sys.executable}" "{script}"')]
    )
    child_hooks = parent.hooks.in_folder(str(child_root))
    await child_hooks.run("after_tool", {}, tool_name="fs_write")
    cwd, workspace = seen.read_text(encoding="utf-8").split("|")
    assert await asyncio.to_thread(os.path.samefile, cwd, child_root)
    assert workspace == str(child_root)
    # The run still lands on the parent's log, and the parent's own runner is unchanged.
    assert parent.hooks.cwd is None
    assert parent.hooks.last


def test_a_windows_hook_timeout_kills_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Note 74: on Windows only the hook's shell was killed, not what it started."""
    import tstd.hooks as hooks
    import tstd.tools.shell as shell

    walked: list[int] = []
    killed: list[int] = []

    class _Proc:
        pid = 4242

        def kill(self) -> None:
            killed.append(self.pid)

    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(shell, "_kill_windows_tree", lambda pid: walked.append(pid))
    hooks._kill(_Proc())  # type: ignore[arg-type]
    assert walked == [4242] and killed == []


async def test_before_tool_context_reaches_a_failed_call(tmp_path: Path) -> None:
    """Note 75: a before_tool hook's context was dropped when the tool failed."""
    import sys

    from tests.test_shell_tools import make_shell_dispatcher
    from tstd.hooks_config import HookEntry, HooksConfig

    script = tmp_path / "hooks-src" / "ctx.py"
    script.parent.mkdir()
    script.write_text(
        'import sys\nsys.stdin.read()\nsys.stdout.write(\'{"context": "repo is frozen"}\')\n',
        encoding="utf-8",
    )
    session = Session(str(tmp_path))
    hook = HookEntry(command=f'"{sys.executable}" "{script}"')
    session.hooks.user = HooksConfig(before_tool=[hook])
    dispatcher = make_shell_dispatcher(tmp_path)

    async def _broken(**_kwargs: object) -> str:
        raise RuntimeError("disk gone")

    dispatcher._handlers["fs_read"] = _broken
    result = await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"}, session)
    assert result.status == "error"
    assert result.output.endswith("[hooks]\nrepo is frozen")


async def test_a_write_stopped_in_its_hooks_is_still_checkpointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Note 76: a Stop during after-write hooks skipped the write's checkpoint."""
    import tstd.tools.dispatch as dispatch_module
    from tests.test_shell_tools import make_shell_dispatcher

    committed: list[list[str]] = []

    class _Checkpointer:
        async def preimage(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def checkpoint(self, paths: list[str], **_kwargs: object) -> object:
            committed.append(paths)
            return None

    async def _stopped(*_args: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(dispatch_module, "run_after_write", _stopped)
    dispatcher = make_shell_dispatcher(tmp_path)
    dispatcher.checkpointer = _Checkpointer()  # type: ignore[assignment]
    session = Session(str(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch("c1", "fs_write", {"path": "a.txt", "content": "x"}, session)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x"
    assert len(committed) == 1


async def test_every_click_that_starts_a_turn_waits_for_a_restore(tmp_path: Path) -> None:
    """Review of 32/36: only ``user_message`` checked the folder-busy guard."""
    import json

    from tests.test_artifacts import _open, _shutdown
    from tstd.daemon import Daemon

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("one\n", encoding="utf-8")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, workspace)
    comment = {
        "id": "c1",
        "path": "a.txt",
        "start_line": 1,
        "end_line": 1,
        "body": "why?",
        "lines": ["one"],
    }
    clicks = [
        {"type": "send_queued", "queued_id": "q1"},
        {"type": "submit_line_comments", "comments": [comment]},
        {"type": "fork_from", "user_index": 0, "content": "again"},
        {"type": "run_grok_command", "name": "plan"},
        {"type": "approve_grok_plan"},
    ]
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        sess.folder_busy = "restoring files"
        for click in clicks:
            raw = json.dumps({**click, "session_id": sid})
            answer = json.loads(await daemon._handle_message(raw, None) or "{}")
            assert answer.get("code") == "session_busy", click["type"]
            assert answer.get("in_reply_to") == click["type"]
            assert "restoring files" in answer.get("message", "")
        assert sess.held_messages == [] and sess._user_message_queue.empty()
    finally:
        await _shutdown(daemon, sid)


async def test_the_loop_starts_no_turn_while_the_folder_is_busy(tmp_path: Path) -> None:
    """Review of 32/36: a schedule or autonomy continue still started a turn."""
    session = Session(str(tmp_path))
    session.folder_busy = "closing its worktree"
    await session.add_user_message("continue")
    waiting = asyncio.create_task(session.wait_for_user_message())
    await asyncio.sleep(0.2)
    assert not waiting.done()
    session.folder_busy = None
    queued = await asyncio.wait_for(waiting, 2)
    assert queued is not None and queued.display == "continue"


async def test_a_held_message_with_run_rules_does_not_steer(tmp_path: Path) -> None:
    """Review of 16: Send on a held ``--deny`` row mid-turn joined a turn without its rules."""
    from tstd.protocol import RunPermissions, RunRule
    from tstd.session import QueuedUserMessage

    session = Session(str(tmp_path))
    rules = RunPermissions(deny=[RunRule(tool="fs_write")])
    held = QueuedUserMessage(display="go", provider="go", run_permissions=rules)
    plain = QueuedUserMessage.plain("also")
    session.held_messages = [held, plain]
    session.open_steering("running-turn")
    assert await session.send_held(held.id)
    assert await session.send_held(plain.id)
    first = session._user_message_queue.get_nowait()
    second = session._user_message_queue.get_nowait()
    assert first.steer_into is None and first.run_permissions == rules
    assert second.steer_into == "running-turn"
