"""Background shell jobs (TD-5137).

Acceptance criteria:

1. ``shell`` takes ``background: true``. It returns a job id, and new tools
   read new output and stop the job. Starting a job is classified like any
   shell call.
2. The daemon owns the jobs (directive 5). It kills them when the session
   ends and lists them in the UI (the ``shell_jobs`` event).
3. A command that hits the foreground timeout can be moved to the background
   instead of being killed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import tstd.shell_read_only as shell_read_only
from tests.test_dispatch import make_classifier
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.autonomy.classifier import RULE_TABLE
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ShellJobInfo, ShellJobs
from tstd.session import Session, SessionRegistry
from tstd.shell_jobs import MAX_RUNNING_JOBS, JobOutput, lost_jobs_after_restart
from tstd.shell_read_only import READ_ONLY_ENTRIES
from tstd.tools import ToolDispatcher, build_decision_request, create_registry
from tstd.tools.delegate import RESEARCH_CHILD_TOOLS, WORKER_CHILD_TOOLS
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.results import HandlerRefusal
from tstd.tools.shell import run_shell
from tstd.tools.shell_job_tools import shell_job_output, shell_job_stop

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

ROOT = Path("/work/proj")


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[Session]:
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = Session(str(ws))
    yield sess
    await sess.shell_jobs.stop_all()


def _job_id(result: str) -> str:
    found = re.search(r"job-\d+", result)
    assert found is not None, result
    return found.group(0)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(check: Any, limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.05)


def _lists(sess: Session) -> list[ShellJobs]:
    return [e for e in sess.event_log.all_events if isinstance(e, ShellJobs)]


# ── AC1: background start, read new output, stop ───────────────────────


@posix_only
async def test_background_returns_a_job_id_at_once(session: Session) -> None:
    started = time.monotonic()
    result = await run_shell(session, "sleep 30", background=True)
    assert time.monotonic() - started < 5
    job_id = _job_id(result)
    assert job_id == "job-1"
    job = session.shell_jobs.get(job_id)
    assert job is not None and job.status == "running"


@posix_only
async def test_shell_output_returns_only_new_output(session: Session) -> None:
    job_id = _job_id(
        await run_shell(
            session,
            "echo one; while [ ! -f go ]; do sleep 0.05; done; echo two >&2; echo three",
            background=True,
        )
    )
    first = await shell_job_output(session=session, job_id=job_id, wait_secs=5)
    assert "one" in first and "two" not in first
    assert f"{job_id}: running" in first
    (Path(session.workspace_path) / "go").write_text("")
    job = session.shell_jobs.get(job_id)
    assert job is not None
    await _until(lambda: job.status == "exited")
    second = await shell_job_output(session=session, job_id=job_id)
    assert "one" not in second
    assert "two" in second and "three" in second
    assert f"{job_id}: exited with code 0" in second
    third = await shell_job_output(session=session, job_id=job_id)
    assert "(none)" in third


@posix_only
async def test_shell_stop_kills_the_whole_process_group(session: Session) -> None:
    ws = Path(session.workspace_path)
    job_id = _job_id(
        await run_shell(
            session,
            "echo $$ > pgid.txt; (sleep 60 & echo $! > child.txt; wait) ; wait",
            background=True,
        )
    )
    await _until(lambda: (ws / "child.txt").exists() and (ws / "child.txt").read_text().strip())
    leader = int((ws / "pgid.txt").read_text())
    child = int((ws / "child.txt").read_text())
    assert _alive(leader) and _alive(child)
    result = await shell_job_stop(session=session, job_id=job_id)
    assert f"{job_id}: stopped" in result
    await _until(lambda: not _alive(child) and not _alive(leader))
    job = session.shell_jobs.get(job_id)
    assert job is not None and job.status == "stopped"
    assert job.watcher is not None and job.watcher.done()


async def test_unknown_job_is_a_typed_refusal(session: Session) -> None:
    with pytest.raises(HandlerRefusal) as refused:
        await shell_job_output(session=session, job_id="job-9")
    assert refused.value.code == "shell_job_not_found"
    with pytest.raises(HandlerRefusal):
        await shell_job_stop(session=session, job_id="job-9")
    assert await shell_job_output(session=session) == "No background jobs in this session."


@posix_only
async def test_listing_without_a_job_id(session: Session) -> None:
    await run_shell(session, "sleep 30", background=True)
    listing = await shell_job_output(session=session)
    assert listing == "job-1: running — sleep 30"


async def test_a_child_and_an_unattended_run_cannot_start_jobs(session: Session) -> None:
    session.delegate_depth = 1
    with pytest.raises(HandlerRefusal) as refused:
        await run_shell(session, "sleep 30", background=True)
    assert refused.value.code == "shell_job_refused"
    session.delegate_depth = 0
    session.autonomy = True
    with pytest.raises(HandlerRefusal):
        await run_shell(session, "sleep 30", background=True)
    assert session.shell_jobs.list() == []


@posix_only
async def test_running_jobs_are_capped(session: Session) -> None:
    for _ in range(MAX_RUNNING_JOBS):
        await run_shell(session, "sleep 30", background=True)
    with pytest.raises(HandlerRefusal) as refused:
        await run_shell(session, "sleep 30", background=True)
    assert "shell_job_stop" in refused.value.message


def test_the_buffer_drops_old_text_and_says_how_much() -> None:
    out = JobOutput(cap=10)
    out.append("0123456789abcdef")
    text, dropped = out.read()
    assert (text, dropped) == ("6789abcdef", 6)
    out.append("XY")
    assert out.read() == ("XY", 0)
    out.append("12345")
    assert out.read(2) == ("12", 0)
    assert out.unread == 3


def test_the_tools_are_offered_to_the_main_loop_only() -> None:
    registry = create_registry()
    shell = registry.require("shell").parameters["properties"]
    assert shell["background"]["type"] == "boolean"
    assert shell["on_timeout"]["enum"] == ["kill", "background"]
    names = {d.function.name for d in registry.to_provider_definitions()}
    assert {"shell_job_output", "shell_job_stop"} <= names
    for child in (WORKER_CHILD_TOOLS, RESEARCH_CHILD_TOOLS):
        assert "shell_job_output" not in child and "shell_job_stop" not in child


# ── AC1: classification ─────────────────────────────────────────────────


@pytest.fixture
def _repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_read_only, "git_ready", lambda: True)
    monkeypatch.setattr(shell_read_only, "_repo_config", {str(ROOT): shell_read_only.RepoConfig()})


def _classify(name: str, arguments: dict[str, Any], *, planning: bool = False) -> Any:
    request = build_decision_request(create_registry().require(name), arguments)
    boundary = Boundary(
        workspace_root=ROOT,
        shell_read_only=frozenset(READ_ONLY_ENTRIES),
        plan_mode=lambda: planning,
    )
    return DecisionClassifier(boundary).classify(request)


@pytest.mark.usefixtures("_repo_config")
@pytest.mark.parametrize(
    "command",
    [
        "npm run dev",
        "git status",
        "ls -la",
        "tail -f log.txt",
        "rm -rf build",
        "echo x > AGENTS.md",
    ],
)
@pytest.mark.parametrize("planning", [False, True])
def test_starting_a_job_is_classified_like_any_shell_call(command: str, planning: bool) -> None:
    plain = _classify("shell", {"command": command}, planning=planning)
    for extra in ({"background": True}, {"on_timeout": "background"}):
        job = _classify("shell", {"command": command, **extra}, planning=planning)
        assert job.decision_class == plain.decision_class, (command, extra)
        assert (job.rule and job.rule.id) == (plain.rule and plain.rule.id)


async def test_a_background_start_still_asks(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = Session(str(ws))
    dispatcher = ToolDispatcher(
        create_registry(), classifier=make_classifier(str(ws)), workspace=ws
    )
    dispatcher.policy = PolicyConfig()
    asked: list[Any] = []

    async def _deny(*args: Any) -> ApprovalOutcome:
        asked.append(args)
        return ApprovalOutcome(False)

    dispatcher.approval_handler = _deny
    register_builtin_handlers(dispatcher)
    result = await dispatcher.dispatch(
        "c1", "shell", {"command": "sleep 30", "background": True}, sess
    )
    assert asked, "a background start must reach the approval gate"
    assert result.decision_class is DecisionClass.B
    assert result.status != "success"
    assert sess.shell_jobs.list() == []


@pytest.mark.parametrize("name", ["shell_job_output", "shell_job_stop"])
def test_reading_and_stopping_a_job_are_class_a(name: str) -> None:
    decision = _classify(name, {"job_id": "job-1"})
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "shell-job"


def test_only_the_builtin_shape_gets_the_grant() -> None:
    from tstd.autonomy.classifier import DecisionRequest

    for request in (
        DecisionRequest(tool_name="shell_job_output", provenance="mcp:srv"),
        DecisionRequest(tool_name="shell_job_stop", provenance="plugin:dist"),
        DecisionRequest(tool_name="shell_job_stop", side_effect_class="ask"),
        DecisionRequest(tool_name="shell_job_output", hosts=frozenset({"example.com"})),
        DecisionRequest(tool_name="shell_job_output", reads=(ROOT / "a",)),
    ):
        decision = DecisionClassifier(Boundary(workspace_root=ROOT)).classify(request)
        assert decision.rule is None or decision.rule.id != "shell-job", request
    ids = [rule.id for rule in RULE_TABLE]
    assert ids.index("side-effect-never") < ids.index("shell-job")
    assert ids.index("shell-job") < ids.index("side-effect-ask-floor")


def test_plan_mode_reads_jobs_but_does_not_stop_them() -> None:
    assert _classify("shell_job_output", {"job_id": "job-1"}, planning=True).decision_class is (
        DecisionClass.A
    )
    stop = _classify("shell_job_stop", {"job_id": "job-1"}, planning=True)
    assert stop.decision_class is DecisionClass.C
    assert stop.rule is not None and stop.rule.id == "plan-mode"


# ── AC2: the session owns the jobs and ends them; the UI lists them ──────


@posix_only
async def test_ending_the_session_kills_its_jobs(session: Session) -> None:
    ws = Path(session.workspace_path)
    await run_shell(session, "echo $$ > a.txt; sleep 60", background=True)
    await run_shell(session, "echo $$ > b.txt; sleep 60", background=True)
    await _until(
        lambda: all((ws / n).exists() and (ws / n).read_text() for n in ("a.txt", "b.txt"))
    )
    pids = [int((ws / n).read_text()) for n in ("a.txt", "b.txt")]
    await session.set_state("running")
    await session.cancel()
    await _until(lambda: not any(_alive(p) for p in pids))
    assert {j.status for j in session.shell_jobs.list()} == {"stopped"}
    late = await run_shell(session, "sleep 1", background=True)
    assert late.startswith("cancelled")
    assert len(session.shell_jobs.list()) == 2
    assert session.shell_jobs.refusal() is not None
    newest = _lists(session)[-1]
    assert [j.status for j in newest.jobs] == ["stopped", "stopped"]


@posix_only
async def test_removing_a_session_kills_its_jobs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    registry = SessionRegistry()
    sess = await registry.create(str(ws))
    await run_shell(sess, "echo $$ > pid.txt; sleep 60", background=True)
    await _until(lambda: (ws / "pid.txt").exists() and (ws / "pid.txt").read_text())
    pid = int((ws / "pid.txt").read_text())
    await registry.remove(sess.id)
    await _until(lambda: not _alive(pid))


@posix_only
async def test_every_change_logs_the_whole_list(session: Session) -> None:
    job_id = _job_id(await run_shell(session, "sleep 30", background=True))
    await run_shell(session, "true", background=True)
    second = session.shell_jobs.get("job-2")
    assert second is not None
    await _until(lambda: second.status == "exited")
    await shell_job_stop(session=session, job_id=job_id)
    lists = _lists(session)
    assert [[(j.job_id, j.status) for j in e.jobs] for e in lists] == [
        [("job-1", "running")],
        [("job-1", "running"), ("job-2", "running")],
        [("job-1", "running"), ("job-2", "exited")],
        [("job-1", "stopped"), ("job-2", "exited")],
    ]
    assert lists[-1].jobs[1].exit_code == 0
    assert lists[-1].jobs[0].command == "sleep 30"
    assert lists[-1].session_id == session.id


def test_a_restart_marks_running_jobs_lost() -> None:
    jobs = [
        ShellJobInfo(job_id="job-1", command="npm run dev", started_at=1.0),
        ShellJobInfo(job_id="job-2", command="true", status="exited", exit_code=0, started_at=2.0),
    ]
    event = ShellJobs(session_id="s", jobs=jobs, seq=3)
    lost = lost_jobs_after_restart([event])
    assert lost is not None
    assert [(j.job_id, j.status) for j in lost] == [("job-1", "lost"), ("job-2", "exited")]
    finished = ShellJobs(session_id="s", jobs=jobs[1:], seq=4)
    assert lost_jobs_after_restart([event, finished]) is None
    assert lost_jobs_after_restart([]) is None


# ── AC3: a timed-out command moves to the background ─────────────────────


@posix_only
async def test_a_timeout_can_move_the_command_to_the_background(session: Session) -> None:
    ws = Path(session.workspace_path)
    result = await run_shell(
        session,
        "echo before; echo $$ > pid.txt; while [ ! -f go ]; do sleep 0.05; done; echo after",
        timeout_secs=1,
        on_timeout="background",
    )
    assert "moved to background job job-1" in result
    assert "before" in result
    assert "exit code" not in result
    pid = int((ws / "pid.txt").read_text())
    assert _alive(pid), "the command must not be killed"
    job = session.shell_jobs.get("job-1")
    assert job is not None
    assert job.info().status == "running"
    (ws / "go").write_text("")
    await _until(lambda: job.status == "exited")
    later = await shell_job_output(session=session, job_id="job-1")
    assert "after" in later and "before" not in later
    assert "exited with code 0" in later


@posix_only
async def test_the_default_timeout_still_kills(session: Session) -> None:
    ws = Path(session.workspace_path)
    result = await run_shell(session, "echo $$ > pid.txt; sleep 60", timeout_secs=1)
    assert "timed out after 1s" in result
    pid = int((ws / "pid.txt").read_text())
    await _until(lambda: not _alive(pid))
    assert session.shell_jobs.list() == []


@posix_only
async def test_a_timeout_in_a_child_is_killed_not_moved(session: Session) -> None:
    session.delegate_depth = 1
    result = await run_shell(session, "sleep 60", timeout_secs=1, on_timeout="background")
    assert "timed out after 1s" in result
    assert session.shell_jobs.list() == []


async def test_a_bad_on_timeout_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="on_timeout"):
        await run_shell(session, "true", on_timeout="detach")  # type: ignore[arg-type]
