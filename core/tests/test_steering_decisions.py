"""TD-5140: only the user's decision writes an agent-proposed steering edit.

A real daemon runs a scripted turn that calls ``propose_steering_edit``;
the window's ``steering_accept`` / ``steering_edit`` / ``steering_reject``
go through the daemon's own message handler. The daemon writes the file,
refuses a stale proposal with a fresh diff, re-checks the path, logs every
decision with its diff, and nothing (skip-all, an autonomy run, a second
click, a phone) accepts on the user's behalf.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_peer_policy import MACHINE, PHONE_VIEWER
from tests.test_two_daemons import _Steps
from tstd import peer_policy
from tstd.autonomy.wakeup import build_wakeup_summary, format_notify_text
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.peer_policy import LOOPBACK_PLAIN
from tstd.protocol import (
    ApprovalRequest,
    SteeringAccept,
    SteeringDecision,
    SteeringEdit,
    SteeringProposal,
    SteeringReject,
    ToolResult,
    TurnComplete,
)
from tstd.session import Session
from tstd.steering_proposals import STALE_MESSAGE, propose

POSIX = os.name == "posix"


def _call(tool: str, **arguments: Any) -> Script:
    return Script(kind="tool_call", tool_name=tool, tool_arguments=json.dumps(arguments))


@dataclass
class Run:
    daemon: Daemon
    sid: str
    ws: Path

    @property
    def session(self) -> Session:
        found = self.daemon.session_registry.get(self.sid)
        assert found is not None
        return found

    def events(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.session.event_log.all_events if isinstance(e, kind)]

    async def turn(self, content: str = "go") -> None:
        before = len(self.events(TurnComplete))
        message = {"type": "user_message", "session_id": self.sid, "content": content}
        await self.daemon._handle_message(json.dumps(message), None)
        await eventually(lambda: len(self.events(TurnComplete)) > before, within=20)

    async def send(self, kind: str, proposal_id: str, **extra: Any) -> dict[str, Any] | None:
        frame = {"type": kind, "session_id": self.sid, "proposal_id": proposal_id, **extra}
        raw = await self.daemon._handle_message(json.dumps(frame), None)
        return None if raw is None else dict(json.loads(raw))


async def _run(tmp_path: Path, steps: list[Script], files: dict[str, str]) -> Run:
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    ws = tmp_path / "ws"
    ws.mkdir()
    for relative, text in files.items():
        (ws / relative).parent.mkdir(parents=True, exist_ok=True)
        (ws / relative).write_text(text, encoding="utf-8")
    sid = await _open(daemon, ws)
    return Run(daemon, sid, ws)


@pytest.fixture
async def proposed(tmp_path: Path) -> AsyncIterator[Run]:
    """One finished turn that proposed a line added to AGENTS.md."""
    old = "# Project\n\n- Use uv.\n"
    steps = [
        _call("propose_steering_edit", path="AGENTS.md", text=old + "- Run ruff first.\n"),
        Script(kind="stream", content="Proposed."),
    ]
    run = await _run(tmp_path, steps, {"AGENTS.md": old})
    try:
        await run.turn("remember to run ruff first, in AGENTS.md")
        yield run
    finally:
        await _shutdown(run.daemon, run.sid)


def _read(run: Run, relative: str = "AGENTS.md") -> str:
    return (run.ws / relative).read_text(encoding="utf-8")


# ── The turn proposes; nothing is written ──────────────────────────────


async def test_the_turn_proposes_with_no_card_and_writes_nothing(proposed: Run) -> None:
    run = proposed
    [result] = run.events(ToolResult)
    assert result.status == "success", result.output
    assert run.events(ApprovalRequest) == []
    [event] = run.events(SteeringProposal)
    assert event.files[0].action == "replace"
    assert _read(run) == "# Project\n\n- Use uv.\n"
    assert run.events(SteeringDecision) == []


# ── Accept, Edit then Save, Reject ─────────────────────────────────────


async def test_accept_writes_the_proposed_text_and_logs_the_diff(proposed: Run) -> None:
    run = proposed
    [event] = run.events(SteeringProposal)
    assert await run.send("steering_accept", event.proposal_id) is None
    assert _read(run) == "# Project\n\n- Use uv.\n- Run ruff first.\n"
    [decision] = run.events(SteeringDecision)
    assert (decision.proposal_id, decision.decision) == (event.proposal_id, "accepted")
    assert decision.files == event.files
    assert run.session.steering_proposals == {}
    again = await run.send("steering_accept", event.proposal_id)
    assert again is not None and again["code"] == "no_steering_proposal"
    assert len(run.events(SteeringDecision)) == 1


async def test_edit_then_save_writes_the_users_text(proposed: Run) -> None:
    run = proposed
    [event] = run.events(SteeringProposal)
    mine = "# Project\n\n- Use uv.\n- Run ruff and mypy first.\n"
    assert await run.send("steering_edit", event.proposal_id, content=mine) is None
    assert _read(run) == mine
    [decision] = run.events(SteeringDecision)
    assert decision.decision == "edited"
    [file] = decision.files
    assert (file.before, file.after) == ("# Project\n\n- Use uv.\n", mine)
    assert "+- Run ruff and mypy first." in file.diff


async def test_an_empty_edit_is_refused_and_the_proposal_stays(proposed: Run) -> None:
    run = proposed
    [event] = run.events(SteeringProposal)
    reply = await run.send("steering_edit", event.proposal_id, content="")
    assert reply is not None and reply["code"] == "steering_edit_empty"
    assert _read(run) == "# Project\n\n- Use uv.\n"
    assert event.proposal_id in run.session.steering_proposals


async def test_reject_writes_nothing_and_logs_the_diff(proposed: Run) -> None:
    run = proposed
    [event] = run.events(SteeringProposal)
    assert await run.send("steering_reject", event.proposal_id) is None
    assert _read(run) == "# Project\n\n- Use uv.\n"
    [decision] = run.events(SteeringDecision)
    assert decision.decision == "rejected"
    assert decision.files == event.files
    late = await run.send("steering_accept", event.proposal_id)
    assert late is not None and late["code"] == "no_steering_proposal"
    assert _read(run) == "# Project\n\n- Use uv.\n"


async def test_save_of_text_the_log_would_blank_is_refused_and_the_proposal_stays(
    proposed: Run,
) -> None:
    """The logged decision must show what was written, so Save refuses it."""
    run = proposed
    [event] = run.events(SteeringProposal)
    mine = "# Project\n\n- Use uv.\n- Check task-tool-availability first.\n"
    reply = await run.send("steering_edit", event.proposal_id, content=mine)
    assert reply is not None and reply["code"] == "steering_secret_shaped"
    assert "change the file by hand" in reply["message"]
    assert _read(run) == "# Project\n\n- Use uv.\n"
    assert run.events(SteeringDecision) == []
    assert event.proposal_id in run.session.steering_proposals


async def test_the_edit_box_text_saves_back_exactly(proposed: Run) -> None:
    """Edit starts from the card's ``after``; saving it unchanged is an Accept."""
    run = proposed
    [event] = run.events(SteeringProposal)
    shown = event.files[0].after
    assert shown == run.session.steering_proposals[event.proposal_id].after
    assert await run.send("steering_edit", event.proposal_id, content=shown) is None
    assert _read(run) == shown
    assert run.events(SteeringDecision)[0].decision == "accepted"


# ── A file that changed is refused with a fresh diff ───────────────────


async def test_a_changed_file_refuses_accept_and_offers_a_fresh_diff(proposed: Run) -> None:
    run = proposed
    [stale] = run.events(SteeringProposal)
    theirs = "# Project\n\n- Use uv.\n- Added by hand.\n"
    (run.ws / "AGENTS.md").write_text(theirs, encoding="utf-8")
    reply = await run.send("steering_accept", stale.proposal_id)
    assert reply is not None
    assert (reply["code"], reply["session_id"]) == ("steering_proposal_stale", run.sid)
    assert reply["message"] == STALE_MESSAGE
    assert "the file changed since this was proposed" in reply["message"].lower()
    assert _read(run) == theirs
    assert run.events(SteeringDecision) == []
    fresh = run.events(SteeringProposal)[-1]
    assert fresh.replaces == stale.proposal_id and fresh.proposal_id != stale.proposal_id
    [file] = fresh.files
    assert (file.before, file.after) == (theirs, stale.files[0].after)
    assert "-- Added by hand." in file.diff
    gone = await run.send("steering_accept", stale.proposal_id)
    assert gone is not None and gone["code"] == "no_steering_proposal"
    assert await run.send("steering_accept", fresh.proposal_id) is None
    assert _read(run) == stale.files[0].after


async def test_a_stale_edit_keeps_the_users_text_in_the_fresh_diff(proposed: Run) -> None:
    run = proposed
    [stale] = run.events(SteeringProposal)
    (run.ws / "AGENTS.md").unlink()
    mine = "# Mine\n"
    reply = await run.send("steering_edit", stale.proposal_id, content=mine)
    assert reply is not None and reply["code"] == "steering_proposal_stale"
    assert not (run.ws / "AGENTS.md").exists()
    fresh = run.events(SteeringProposal)[-1]
    assert (fresh.files[0].action, fresh.files[0].after) == ("create", mine)


async def test_a_file_changed_to_what_the_card_would_blank_gets_no_fresh_diff(
    proposed: Run,
) -> None:
    run = proposed
    [stale] = run.events(SteeringProposal)
    theirs = "# Project\n\nSee docs/tst-desk-gap-audit-2026-10-14.md first.\n"
    (run.ws / "AGENTS.md").write_text(theirs, encoding="utf-8")
    reply = await run.send("steering_accept", stale.proposal_id)
    assert reply is not None and reply["code"] == "steering_secret_shaped"
    assert "Reject this proposal" in reply["message"]
    assert _read(run) == theirs
    assert run.events(SteeringProposal) == [stale]
    assert list(run.session.steering_proposals) == [stale.proposal_id]
    assert await run.send("steering_reject", stale.proposal_id) is None
    assert run.events(SteeringDecision)[0].decision == "rejected"
    assert _read(run) == theirs


@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
async def test_a_symlink_swapped_in_after_the_proposal_is_refused(tmp_path: Path) -> None:
    steps = [
        _call("propose_steering_edit", path=".tst/rules/style.md", text="# Style\n"),
        Script(kind="stream", content="Proposed."),
    ]
    run = await _run(tmp_path, steps, {})
    try:
        await run.turn()
        [event] = run.events(SteeringProposal)
        outside = tmp_path / "outside"
        outside.mkdir()
        (run.ws / ".tst" / "rules").mkdir(parents=True, exist_ok=True)
        (run.ws / ".tst" / "rules").rmdir()
        (run.ws / ".tst" / "rules").symlink_to(outside, target_is_directory=True)
        reply = await run.send("steering_accept", event.proposal_id)
        assert reply is not None and reply["code"] == "outside_workspace"
        assert list(outside.iterdir()) == []
        assert event.proposal_id in run.session.steering_proposals
    finally:
        await _shutdown(run.daemon, run.sid)


async def test_a_session_moved_since_the_proposal_is_refused(proposed: Run, tmp_path: Path) -> None:
    run = proposed
    [event] = run.events(SteeringProposal)
    elsewhere = tmp_path / "ws2"
    elsewhere.mkdir()
    (elsewhere / "AGENTS.md").write_text("# Project\n\n- Use uv.\n", encoding="utf-8")
    move = {"type": "move_session", "session_id": run.sid, "workspace_path": str(elsewhere)}
    await run.daemon._handle_message(json.dumps(move), None)
    reply = await run.send("steering_accept", event.proposal_id)
    assert reply is not None and reply["code"] == "steering_workspace_moved"
    assert (elsewhere / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n\n- Use uv.\n"
    assert await run.send("steering_reject", event.proposal_id) is None


# ── Nothing accepts for the user ───────────────────────────────────────


async def test_skip_all_neither_accepts_nor_opens_steering_to_writes(tmp_path: Path) -> None:
    steps = [
        _call("fs_write", path="AGENTS.md", content="pwned\n"),
        _call("propose_steering_edit", path="AGENTS.md", text="# Proposed\n"),
        Script(kind="stream", content="Done."),
    ]
    run = await _run(tmp_path, steps, {"AGENTS.md": "# Mine\n"})
    run.daemon.skip_all_approvals = True
    try:
        await run.turn()
        write, proposal = run.events(ToolResult)
        assert write.error_code == "boundary_refusal"
        assert proposal.status == "success"
        assert _read(run) == "# Mine\n"
        assert len(run.session.steering_proposals) == 1
        assert run.events(SteeringDecision) == []
    finally:
        await _shutdown(run.daemon, run.sid)


async def test_an_autonomy_run_parks_the_proposal_for_its_summary(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    session = Session(str(ws))
    session.autonomy = True
    session.autonomy_stop_reason = "definition of done met"
    parked = await propose(session, "AGENTS.md", "# Starter\n", "call_9")
    [event] = [e for e in session.event_log.all_events if isinstance(e, SteeringProposal)]
    assert event.autonomy is True and parked.autonomy is True
    assert not (ws / "AGENTS.md").exists()
    summary = await build_wakeup_summary(session)
    assert summary.steering_proposals == ["AGENTS.md (create)"]
    text = format_notify_text(summary)
    assert "Steering edits waiting for you:\n- AGENTS.md (create)" in text
    assert not (ws / "AGENTS.md").exists()
    assert list(session.steering_proposals) == [parked.proposal_id]


# ── Who may decide ─────────────────────────────────────────────────────


def test_accept_and_save_are_this_computers_reject_is_open_to_a_phone() -> None:
    accept = SteeringAccept(session_id="s1", proposal_id="sp-1")
    edit = SteeringEdit(session_id="s1", proposal_id="sp-1", content="x")
    reject = SteeringReject(session_id="s1", proposal_id="sp-1")
    for msg in (accept, edit):
        assert peer_policy.check(msg, LOOPBACK_PLAIN, bridged=False) is None
        for peer in (PHONE_VIEWER, MACHINE):
            refusal = peer_policy.check(msg, peer, bridged=False)
            assert refusal is not None and json.loads(refusal)["code"] == "loopback_only"
    assert peer_policy.check(reject, LOOPBACK_PLAIN, bridged=False) is None
    assert peer_policy.check(reject, PHONE_VIEWER, bridged=False) is None
    machine = peer_policy.check(reject, MACHINE, bridged=False)
    assert machine is not None and json.loads(machine)["code"] == "not_allowed"
    assert not {"steering_accept", "steering_edit", "steering_reject"} & set(
        peer_policy.FORWARDED_VERBS
    )


# ── The two appendix gaps, end to end ──────────────────────────────────


async def test_a_starter_agents_md_from_a_scan_of_the_repo(tmp_path: Path) -> None:
    starter = "# Widget\n\n## Commands\n\n- Test: `make test`\n"
    steps = [
        _call("fs_list", path="."),
        _call("fs_read", path="README.md"),
        _call("propose_steering_edit", path="AGENTS.md", text=starter),
        Script(kind="stream", content="Here is a starter AGENTS.md for you to review."),
    ]
    run = await _run(tmp_path, steps, {"README.md": "# Widget\n\nRun make test.\n"})
    try:
        await run.turn("write a starter AGENTS.md for this repo")
        listed, read, proposal = run.events(ToolResult)
        assert [r.status for r in (listed, read, proposal)] == ["success"] * 3
        assert run.events(ApprovalRequest) == []
        assert not (run.ws / "AGENTS.md").exists()
        [event] = run.events(SteeringProposal)
        assert event.files[0].action == "create"
        assert await run.send("steering_accept", event.proposal_id) is None
        assert _read(run) == starter
    finally:
        await _shutdown(run.daemon, run.sid)


async def test_the_session_log_keeps_proposals_and_decisions_across_a_restart(
    tmp_path: Path,
) -> None:
    steps = [
        _call("propose_steering_edit", path="CLAUDE.md", text="# Rules\n"),
        Script(kind="stream", content="Proposed."),
    ]
    run = await _run(tmp_path, steps, {})
    await run.turn()
    [first] = run.events(SteeringProposal)
    assert await run.send("steering_reject", first.proposal_id) is None
    await _shutdown(run.daemon, run.sid)
    revived = Daemon(data_dir=tmp_path / "data", provider=_Steps([]))
    try:
        await revived._restore_sessions()
        session = revived.session_registry.get(run.sid)
        assert session is not None
        events = session.event_log.all_events
        [logged] = [e for e in events if isinstance(e, SteeringDecision)]
        assert (logged.decision, logged.files[0].diff) == ("rejected", first.files[0].diff)
        assert [e.proposal_id for e in events if isinstance(e, SteeringProposal)] == [
            first.proposal_id
        ]
        # Parked proposals live in memory: after a restart none is open.
        assert session.steering_proposals == {}
        frame = {"type": "steering_accept", "session_id": run.sid, "proposal_id": first.proposal_id}
        raw = await revived._handle_message(json.dumps(frame), None)
        assert raw is not None and json.loads(raw)["code"] == "no_steering_proposal"
        assert not (run.ws / "CLAUDE.md").exists()
    finally:
        await _shutdown(revived, run.sid)


async def _restart(run: Run, tmp_path: Path, *, keep_conversation: bool = True) -> Daemon:
    await _shutdown(run.daemon, run.sid)
    if not keep_conversation:
        (run.daemon._session_persist.dir_for(run.sid) / "conversation.json").unlink()
    revived = Daemon(data_dir=tmp_path / "data", provider=_Steps([]))
    await revived._restore_sessions()
    return revived


@pytest.mark.parametrize("keep_conversation", [True, False], ids=["revived", "replay-only"])
async def test_a_restart_closes_open_proposals_in_the_log(
    tmp_path: Path, keep_conversation: bool
) -> None:
    """A card the old process held must not come back with Accept live."""
    steps = [
        _call("propose_steering_edit", path="AGENTS.md", text="# Mine\n- Run ruff.\n"),
        _call("propose_steering_edit", path="CLAUDE.md", text="# Rules\n"),
        Script(kind="stream", content="Proposed."),
    ]
    run = await _run(tmp_path, steps, {"AGENTS.md": "# Mine\n"})
    await run.turn()
    first, second = run.events(SteeringProposal)
    assert await run.send("steering_reject", second.proposal_id) is None
    revived = await _restart(run, tmp_path, keep_conversation=keep_conversation)
    try:
        session = revived.session_registry.get(run.sid)
        assert session is not None
        decisions = {
            e.proposal_id: e
            for e in session.event_log.all_events
            if isinstance(e, SteeringDecision)
        }
        assert decisions[second.proposal_id].decision == "rejected"
        expired = decisions[first.proposal_id]
        assert (expired.decision, expired.files) == ("expired", first.files)
        frame = {"type": "steering_accept", "session_id": run.sid, "proposal_id": first.proposal_id}
        raw = await revived._handle_message(json.dumps(frame), None)
        assert raw is not None
        reply = json.loads(raw)
        assert reply["code"] == "no_steering_proposal"
        assert "nothing was written" in reply["message"]
        assert (run.ws / "AGENTS.md").read_text(encoding="utf-8") == "# Mine\n"
    finally:
        await _shutdown(revived, run.sid)
    if not keep_conversation:
        return
    again = Daemon(data_dir=tmp_path / "data", provider=_Steps([]))
    try:
        await again._restore_sessions()
        session = again.session_registry.get(run.sid)
        assert session is not None
        closing = [
            e.proposal_id
            for e in session.event_log.all_events
            if isinstance(e, SteeringDecision) and e.decision == "expired"
        ]
        assert closing == [first.proposal_id]
    finally:
        await _shutdown(again, run.sid)
