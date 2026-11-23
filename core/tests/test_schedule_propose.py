"""TD-5263: the agent proposes a scheduled job; only the person saves it.

``schedule_propose`` writes nothing. These pin that, the narrow draft, who
is refused, Class A with no approval card, that a forged save is refused,
that the daemon and not the client marks a job as agent-proposed, and that
such a job does not run under skip-all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_classifier import WS, boundary, classify, fired_as, req
from tests.test_dispatch import make_classifier
from tstd.autonomy import Boundary, DecisionClass
from tstd.autonomy.classifier import PLAN_READ_ONLY_BUILTINS
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ScheduleProposal, ScheduleProposalDecision
from tstd.scheduler.models import JobDraft
from tstd.scheduler.pin import bind_scheduled_pin, current_scheduled_pin, reset_scheduled_pin
from tstd.scheduler.proposals import MAX_OPEN, expire_restored, settle
from tstd.scheduler.store import list_jobs, save_job
from tstd.session import Session
from tstd.tools import ToolDispatcher
from tstd.tools.boundary import PathGuard
from tstd.tools.delegate import WORKER_CHILD_TOOLS
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import create_registry

ASK = {
    "instruction": "Run the dependency check and list what is out of date.",
    "cadence": "weekdays at 8:00",
}


@dataclass
class Env:
    ws: Path
    data: Path
    session: Session
    dispatcher: ToolDispatcher
    approvals: list[tuple[Any, ...]] = field(default_factory=list)

    def cards(self) -> list[ScheduleProposal]:
        return [e for e in self.session.event_log.all_events if isinstance(e, ScheduleProposal)]

    async def call(self, name: str = "schedule_propose", **arguments: Any) -> Any:
        return await self.dispatcher.dispatch("call_1", name, arguments, self.session)


def _env(tmp_path: Path) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    session = Session(str(ws))
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=make_classifier(str(ws)),
        path_guard=PathGuard(Boundary(workspace_root=ws)),
        workspace=ws,
    )
    dispatcher.policy = PolicyConfig()
    env = Env(ws, data, session, dispatcher)

    async def _approve(*args: Any) -> ApprovalOutcome:
        env.approvals.append(args)
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    register_builtin_handlers(dispatcher, data_dir=data)
    return env


def _job(data: Path, workspace: Path, text: str, **over: Any) -> Any:
    draft = JobDraft(
        workspace=str(workspace), instruction=text, cadence="every 2 hours", deliver_to="window"
    )
    return save_job(data, draft.model_copy(update=over))


# ── The tool ───────────────────────────────────────────────────────────


async def test_a_proposal_is_a_card_and_nothing_else(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = await env.call(**ASK, timezone="Europe/London")
    assert result.status == "success", result.output
    assert result.decision_class is DecisionClass.A and env.approvals == []
    assert "Nothing was scheduled" in result.output
    [card] = env.cards()
    assert card.instruction == ASK["instruction"]
    assert (card.cadence, card.timezone, card.next_run) == ("0 8 * * 1-5", "Europe/London", None)
    assert card.workspace == str(env.ws) and card.tool_call_id == "call_1"
    assert len(card.next_runs) == 3
    assert list(env.session.schedule_proposals) == [card.proposal_id]
    # No job, no file: the scheduler's store was not touched.
    assert list_jobs(env.data) == []
    assert list(env.data.iterdir()) == []


async def test_a_one_off_is_proposed_by_its_time(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = await env.call(instruction="Remind me.", run_at="2030-01-02T09:00:00Z")
    assert result.status == "success", result.output
    [card] = env.cards()
    assert card.cadence is None and card.next_run is not None
    assert card.next_runs == [card.next_run]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"instruction": "x"}, "bad_request"),
        (
            {"instruction": "x", "cadence": "every 2 hours", "run_at": "2030-01-02T09:00:00Z"},
            "bad_request",
        ),
        ({"instruction": "x", "cadence": "whenever"}, "bad_request"),
        ({"instruction": "x", "cadence": "every 5 minutes"}, "schedule_too_often"),
        ({"instruction": "x", "cadence": "*/5 * * * *"}, "schedule_too_often"),
        (
            {"instruction": "x", "cadence": "every 2 hours", "timezone": "Mars/Olympus"},
            "bad_request",
        ),
        ({"instruction": "   ", "cadence": "every 2 hours"}, "bad_request"),
    ],
)
async def test_what_the_tool_refuses(tmp_path: Path, arguments: dict[str, Any], code: str) -> None:
    env = _env(tmp_path)
    result = await env.call(**arguments)
    assert result.status != "success" and result.error_code == code, result.output
    assert env.cards() == [] and env.session.schedule_proposals == {}


async def test_every_fifteen_minutes_is_the_most_often(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert (await env.call(instruction="x", cadence="every 15 minutes")).status == "success"


@pytest.mark.parametrize(
    "extra", ["workspace", "deliver_to", "preset", "then", "engine", "email_to"]
)
async def test_the_model_cannot_set_what_is_the_persons_to_set(tmp_path: Path, extra: str) -> None:
    env = _env(tmp_path)
    result = await env.call(**ASK, **{extra: "/etc"})
    assert result.status != "success", extra
    assert env.cards() == []


async def test_a_secret_in_the_instruction_is_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    secret = "sk-" + "a1B2c3D4" * 6
    result = await env.call(instruction=f"Call the API with {secret}", cadence="every 2 hours")
    assert result.status != "success"
    assert secret not in result.output and env.cards() == []


async def test_nobody_unattended_may_propose(tmp_path: Path) -> None:
    for attribute, value in (("delegate_depth", 1), ("autonomy", True), ("scheduled_run", True)):
        home = tmp_path / attribute
        home.mkdir()
        env = _env(home)
        setattr(env.session, attribute, value)
        result = await env.call(**ASK)
        assert result.error_code == "schedule_unavailable", attribute
        assert env.cards() == []
    # ``tst run --on-ask deny-and-continue`` is unattended by declaration.
    home = tmp_path / "deny"
    home.mkdir()
    env = _env(home)
    env.session.run_permissions = SimpleNamespace(on_ask="deny_and_continue")  # type: ignore[attr-defined]
    assert (await env.call(**ASK)).error_code == "schedule_unavailable"
    assert env.cards() == []


def test_plan_mode_refuses_a_proposal_but_not_the_list() -> None:
    planning = replace(boundary(), plan_mode=lambda: True)
    refused = classify(planning, req(tool_name="schedule_propose"))
    listed = classify(planning, req(tool_name="schedule_list"))
    assert refused.decision_class is DecisionClass.C and fired_as(refused, "plan-mode")
    assert listed.decision_class is DecisionClass.A


async def test_a_restart_expires_open_proposals_once(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for _ in range(2):
        assert (await env.call(**ASK)).status == "success"
    first, second = (c.proposal_id for c in env.cards())
    await settle(env.session, first, "dismissed")
    logged = list(env.session.event_log.all_events)
    revived = Session(str(env.ws))
    assert await expire_restored(revived, logged) == 1
    ends = [e for e in revived.event_log.all_events if isinstance(e, ScheduleProposalDecision)]
    assert [(d.proposal_id, d.decision) for d in ends] == [(second, "expired")]
    # Replayed again with its own expiry in the log: nothing more to close.
    assert await expire_restored(revived, logged + ends) == 0


async def test_three_open_proposals_is_the_most(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for _ in range(MAX_OPEN):
        assert (await env.call(**ASK)).status == "success"
    assert (await env.call(**ASK)).error_code == "schedule_limit"
    first = env.cards()[0].proposal_id
    assert await settle(env.session, first, "dismissed") is not None
    assert (await env.call(**ASK)).status == "success"
    decisions = [
        e for e in env.session.event_log.all_events if isinstance(e, ScheduleProposalDecision)
    ]
    assert [(d.proposal_id, d.decision) for d in decisions] == [(first, "dismissed")]
    assert await settle(env.session, first, "dismissed") is None


async def test_the_list_is_this_workspaces_jobs_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    assert (await env.call("schedule_list")).output == "No scheduled jobs in this workspace."
    mine = _job(env.data, env.ws, "Mine: " + "x" * 400)
    _job(env.data, other, "Somebody else's job")
    result = await env.call("schedule_list")
    assert result.status == "success" and result.decision_class is DecisionClass.A
    assert env.approvals == []
    assert mine.id in result.output and "Somebody else" not in result.output
    assert len(result.output) < 400 and result.output.rstrip().endswith("…")


def test_only_the_builtins_get_the_rule_and_children_get_neither() -> None:
    for name in ("schedule_propose", "schedule_list"):
        decision = classify(boundary(), req(tool_name=name))
        assert decision.decision_class is DecisionClass.A and fired_as(
            decision, "schedule-proposal"
        )
        for no_grant in (
            req(tool_name=name, provenance="mcp:srv"),
            req(tool_name=name, provenance="plugin:dist"),
            req(tool_name=name, reads=(WS / "a",)),
            req(tool_name=name, writes=(WS / "a",)),
            req(tool_name=name, hosts=frozenset({"example.com"})),
            req(tool_name=name, is_mutation=True),
        ):
            assert not fired_as(classify(boundary(), no_grant), "schedule-proposal"), no_grant
        assert name not in WORKER_CHILD_TOOLS
    # Reading the list is fine while planning; proposing a job is not.
    assert "schedule_list" in PLAN_READ_ONLY_BUILTINS
    assert "schedule_propose" not in PLAN_READ_ONLY_BUILTINS


def test_an_agent_proposed_job_does_not_run_under_skip_all(tmp_path: Path) -> None:
    mine = _job(tmp_path, tmp_path, "Mine")
    assert (mine.origin, mine.allow_skip_all) == ("user", False)
    proposed = mine.model_copy(update={"origin": "agent"})
    allowed = proposed.model_copy(update={"allow_skip_all": True})
    seen = []
    for job in (mine, proposed, allowed):
        token = bind_scheduled_pin(job)
        seen.append(current_scheduled_pin().ignore_skip_all)
        reset_scheduled_pin(token)
    assert seen == [False, True, False]
    assert current_scheduled_pin().ignore_skip_all is False


# ── The daemon: Save, Dismiss, and what a client cannot assert ─────────


async def _proposing_daemon(tmp_path: Path) -> tuple[Any, Any, Any, Session, str]:
    from tests.test_add_model import _ask
    from tests.test_models_on_phone import _seeded

    daemon, task, ws = await _seeded(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    opened = await _ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    sess = daemon.session_registry.get(opened["session_id"])
    assert sess is not None
    tool = sess_dispatcher_call(daemon, sess)
    result = await tool("schedule_propose", ASK)
    assert result.status == "success", result.output
    [proposal_id] = list(sess.schedule_proposals)
    return daemon, task, ws, sess, proposal_id


def sess_dispatcher_call(daemon: Any, sess: Session) -> Any:
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=make_classifier(sess.workspace_path),
        path_guard=PathGuard(Boundary(workspace_root=Path(sess.workspace_path))),
        workspace=Path(sess.workspace_path),
    )
    dispatcher.policy = PolicyConfig()
    register_builtin_handlers(dispatcher, data_dir=daemon.data_dir)

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return await dispatcher.dispatch("c1", name, arguments, sess)

    return call


def _save(sess: Session, proposal_id: str, **over: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "save_job",
        "workspace": sess.project_path,
        "instruction": ASK["instruction"],
        "cadence": "0 8 * * 1-5",
        "deliver_to": "window",
        "proposal_id": proposal_id,
        "proposal_session_id": sess.id,
    }
    frame.update(over)
    return frame


async def test_only_the_persons_save_creates_the_job_and_the_daemon_marks_it(
    tmp_path: Path,
) -> None:
    from tests.test_add_model import _ask, _stop

    daemon, task, ws, sess, proposal_id = await _proposing_daemon(tmp_path)
    try:
        # Skip-all on: still no job. There is no approval for it to skip.
        await _ask(ws, {"type": "set_skip_all_approvals", "enabled": True}, "setup_state")
        assert list_jobs(daemon.data_dir) == []
        assert proposal_id in sess.schedule_proposals
        # The save may try to aim the job elsewhere; it is the proposer's project regardless.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        listed = await _ask(ws, _save(sess, proposal_id, workspace=str(elsewhere)), "job_list")
        assert listed["type"] == "job_list", listed
        [job] = list_jobs(daemon.data_dir)
        assert (job.origin, job.origin_session_id) == ("agent", sess.id)
        assert Path(job.workspace) == Path(sess.project_path)
        assert job.allow_skip_all is False
        assert sess.schedule_proposals == {}
        decisions = [
            e for e in sess.event_log.all_events if isinstance(e, ScheduleProposalDecision)
        ]
        assert [(d.decision, d.job_id) for d in decisions] == [("saved", job.id)]
        # Used once: the same proposal cannot make a second job.
        again = await _ask(ws, _save(sess, proposal_id), "job_list")
        assert again["type"] == "error" and again["code"] == "proposal_not_found"
        assert len(list_jobs(daemon.data_dir)) == 1
        # The window learns who wrote it, and a pause does not launder the origin.
        [entry] = listed["jobs"]
        assert (entry["origin"], entry["allow_skip_all"]) == ("agent", False)
        await _ask(ws, {"type": "save_job", "id": job.id, "paused": True}, "job_list")
        [paused] = list_jobs(daemon.data_dir)
        assert paused.paused and (paused.origin, paused.origin_session_id) == ("agent", sess.id)
        # The person's tick, and only that, lets it run under skip-all.
        ticked = {"type": "save_job", "id": job.id, "allow_skip_all": True}
        await _ask(ws, ticked, "job_list")
        [allowed] = list_jobs(daemon.data_dir)
        assert allowed.allow_skip_all and allowed.origin == "agent"
    finally:
        await _stop(task, ws)


async def test_a_forged_or_stale_proposal_schedules_nothing(tmp_path: Path) -> None:
    from tests.test_add_model import _ask, _stop

    daemon, task, ws, sess, proposal_id = await _proposing_daemon(tmp_path)
    try:
        forged = [
            _save(sess, "sp_000000000000"),
            _save(sess, proposal_id, proposal_session_id="no-such-session"),
            {**_save(sess, proposal_id), "proposal_session_id": None},
            {**_save(sess, proposal_id), "proposal_id": None},
            _save(sess, proposal_id, id="some-existing-job"),
        ]
        for frame in forged:
            reply = await _ask(ws, frame, "job_list")
            assert reply["type"] == "error" and reply["code"] == "proposal_not_found", frame
        assert list_jobs(daemon.data_dir) == []
        assert proposal_id in sess.schedule_proposals
        # An ordinary save says nothing about a proposal and is the person's own job.
        plain = _save(sess, proposal_id)
        del plain["proposal_id"], plain["proposal_session_id"]
        assert (await _ask(ws, plain, "job_list"))["type"] == "job_list"
        [job] = list_jobs(daemon.data_dir)
        assert (job.origin, job.origin_session_id) == ("user", None)
        assert proposal_id in sess.schedule_proposals
    finally:
        await _stop(task, ws)


async def test_dismiss_schedules_nothing_and_closes_the_card(tmp_path: Path) -> None:
    from tests.test_add_model import _ask, _stop

    daemon, task, ws, sess, proposal_id = await _proposing_daemon(tmp_path)
    try:
        frame = {
            "type": "dismiss_schedule_proposal",
            "session_id": sess.id,
            "proposal_id": proposal_id,
        }
        await ws.send(json.dumps(frame))
        for _ in range(100):
            if not sess.schedule_proposals:
                break
            await __import__("asyncio").sleep(0.02)
        assert sess.schedule_proposals == {} and list_jobs(daemon.data_dir) == []
        decisions = [
            e for e in sess.event_log.all_events if isinstance(e, ScheduleProposalDecision)
        ]
        assert [(d.decision, d.job_id) for d in decisions] == [("dismissed", None)]
        gone = await _ask(ws, frame, "schedule_proposal_decision")
        assert gone["type"] == "error" and gone["code"] == "proposal_not_found"
    finally:
        await _stop(task, ws)


@pytest.mark.parametrize("ignores", [False, True])
async def test_skip_all_is_not_in_force_for_a_session_that_ignores_it(
    tmp_path: Path, ignores: bool
) -> None:
    """A real turn: with skip-all on, a shell command runs unasked; in the run of an
    agent-proposed job the same command parks on an approval card."""
    from tests.machine_helpers import eventually
    from tests.test_artifacts import _open, _shutdown
    from tests.test_two_daemons import _Steps
    from tstd.daemon import Daemon
    from tstd.mock import Script
    from tstd.protocol import ApprovalRequest, ToolResult

    call = json.dumps({"command": "touch out.txt"})
    steps = [
        Script(kind="tool_call", tool_name="shell", tool_arguments=call),
        Script(kind="stream", content="Done."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    daemon.skip_all_approvals = True
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        session.ignores_skip_all = ignores
        message = {"type": "user_message", "session_id": sid, "content": "write it"}
        await daemon._handle_message(json.dumps(message), None)
        log = session.event_log

        def settled() -> bool:
            return any(isinstance(e, ApprovalRequest | ToolResult) for e in log.all_events)

        await eventually(settled, within=20)
        asked = [e for e in log.all_events if isinstance(e, ApprovalRequest)]
        if ignores:
            assert len(asked) == 1 and not (ws / "out.txt").exists()
        else:
            assert asked == [] and (ws / "out.txt").exists()
    finally:
        await _shutdown(daemon, sid)
