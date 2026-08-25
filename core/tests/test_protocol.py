"""Tests for the protocol schema — round-trip serialization for every message type."""

from __future__ import annotations

import json
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from tstd.protocol import (
    _KNOWN_CLIENT_TYPES,
    _KNOWN_EVENT_TYPES,
    PROTOCOL_VERSION,
    AlwaysAllow,
    ApprovalRequest,
    Approve,
    ArchiveSession,
    AssistantDelta,
    AssistantReasoning,
    Attach,
    Cancel,
    ClientMessageT,
    CostUpdate,
    CuKillState,
    CuSession,
    DaemonEvent,
    DaemonEventT,
    DecisionLogged,
    DeleteSession,
    Deny,
    Detach,
    EndSession,
    Error,
    ExportUsage,
    GetInstructionStack,
    GetUsage,
    HandshakeError,
    Hello,
    ListPolicyRules,
    MemoryAccept,
    MemoryEdit,
    MemoryFileEdit,
    MemoryProposal,
    MemoryReject,
    MoveSession,
    NewSession,
    OpenWorkspace,
    PolicyRules,
    PolicyRuleSummary,
    Ready,
    RenameSession,
    Resume,
    RevokePolicyRule,
    SessionState,
    SetCoworker,
    SetCuIndicators,
    SetCuKill,
    SetLoadGlobalMemory,
    SetRemoteAttach,
    SetSessionStar,
    SetSkipAllApprovals,
    SetTier,
    SetWorkspacePin,
    ShellOutput,
    ToolCall,
    ToolResult,
    TurnComplete,
    UnknownMessageTypeError,
    UsageExported,
    UsageReport,
    UsageRollup,
    UserMessage,
    build_error,
    build_hello_ack,
    parse_client_message,
    parse_daemon_event,
    parse_hello,
)


def _roundtrip(msg: Any) -> Any:
    """Serialize and parse back, then assert the type matches."""
    raw = msg.model_dump_json()
    parsed = parse_daemon_event(raw) if isinstance(msg, DaemonEvent) else parse_client_message(raw)
    assert type(parsed) is type(msg), f"Expected {type(msg).__name__}, got {type(parsed).__name__}"
    return parsed


# ── Client → Daemon ────────────────────────────────────────────────────


class TestClientMessages:
    def test_hello(self) -> None:
        msg = Hello(token="abc123", version=1)
        back = _roundtrip(msg)
        assert isinstance(back, Hello)
        assert back.token == "abc123"
        assert back.version == 1

    def test_open_workspace(self) -> None:
        msg = OpenWorkspace(path="/home/user/project")
        back = _roundtrip(msg)
        assert isinstance(back, OpenWorkspace)
        assert back.path == "/home/user/project"

    def test_user_message(self) -> None:
        msg = UserMessage(session_id="sess-1", content="hello world")
        back = _roundtrip(msg)
        assert isinstance(back, UserMessage)
        assert back.session_id == "sess-1"
        assert back.content == "hello world"

    def test_approve(self) -> None:
        msg = Approve(session_id="sess-1", tool_call_id="tc-1")
        back = _roundtrip(msg)
        assert isinstance(back, Approve)
        assert back.tool_call_id == "tc-1"

    def test_deny(self) -> None:
        msg = Deny(session_id="sess-1", tool_call_id="tc-1", reason="not safe")
        back = _roundtrip(msg)
        assert isinstance(back, Deny)
        assert back.reason == "not safe"

    def test_always_allow(self) -> None:
        msg = AlwaysAllow(session_id="sess-1", tool_call_id="tc-1")
        back = _roundtrip(msg)
        assert isinstance(back, AlwaysAllow)
        assert back.tool_call_id == "tc-1"

    def test_list_policy_rules(self) -> None:
        msg = ListPolicyRules(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, ListPolicyRules)

    def test_revoke_policy_rule(self) -> None:
        msg = RevokePolicyRule(session_id="sess-1", tool="shell", args="rm *")
        back = _roundtrip(msg)
        assert isinstance(back, RevokePolicyRule)
        assert back.tool == "shell"
        assert back.args == "rm *"

    def test_set_skip_all_approvals(self) -> None:
        msg = SetSkipAllApprovals(enabled=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetSkipAllApprovals)
        assert back.enabled is True
        # Machine-wide: a session_id would make a clone-local setting.
        assert "session_id" not in SetSkipAllApprovals.model_fields

    def test_set_load_global_memory(self) -> None:
        msg = SetLoadGlobalMemory(enabled=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetLoadGlobalMemory)
        assert back.enabled is True
        assert "session_id" not in SetLoadGlobalMemory.model_fields

    def test_set_coworker(self) -> None:
        msg = SetCoworker(enabled=False)
        back = _roundtrip(msg)
        assert isinstance(back, SetCoworker)
        assert back.enabled is False
        assert "session_id" not in SetCoworker.model_fields

    def test_set_cu_indicators(self) -> None:
        msg = SetCuIndicators(glow=False, agent_cursor=True, show_on_real_display=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetCuIndicators)
        assert back.glow is False
        assert back.agent_cursor is True
        assert back.show_on_real_display is True
        assert "session_id" not in SetCuIndicators.model_fields

    def test_set_workspace_pin(self) -> None:
        msg = SetWorkspacePin(path="/ws", pinned=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetWorkspacePin)
        assert back.pinned is True
        assert "session_id" not in SetWorkspacePin.model_fields

    def test_set_cu_kill(self) -> None:
        msg = SetCuKill(killed=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetCuKill)
        assert back.killed is True
        # Process-wide: a session_id would make a per-session latch.
        assert "session_id" not in SetCuKill.model_fields

    def test_set_remote_attach(self) -> None:
        msg = SetRemoteAttach(enabled=True)
        back = _roundtrip(msg)
        assert isinstance(back, SetRemoteAttach)
        assert back.enabled is True
        assert "session_id" not in SetRemoteAttach.model_fields

    def test_resume(self) -> None:
        msg = Resume(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Resume)
        assert back.session_id == "sess-1"

    def test_deny_no_reason(self) -> None:
        msg = Deny(session_id="sess-1", tool_call_id="tc-1")
        back = _roundtrip(msg)
        assert isinstance(back, Deny)
        assert back.reason is None

    def test_cancel(self) -> None:
        msg = Cancel(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Cancel)

    def test_attach(self) -> None:
        msg = Attach(session_id="sess-1", from_seq=5)
        back = _roundtrip(msg)
        assert isinstance(back, Attach)
        assert back.from_seq == 5

    def test_attach_default_seq(self) -> None:
        msg = Attach(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Attach)
        assert back.from_seq == 1

    def test_detach(self) -> None:
        msg = Detach(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Detach)

    def test_set_tier(self) -> None:
        msg = SetTier(session_id="sess-1", tier="brain")
        back = _roundtrip(msg)
        assert isinstance(back, SetTier)
        assert back.tier == "brain"
        for t in ("brain", "worker", "validator"):
            m = SetTier(session_id="sess-1", tier=t)
            r = _roundtrip(m)
            assert isinstance(r, SetTier)
            assert r.tier == t

    def test_set_tier_invalid(self) -> None:
        with pytest.raises(ValidationError):
            SetTier(session_id="sess-1", tier="superbrain")  # type: ignore[arg-type]

    def test_get_instruction_stack(self) -> None:
        msg = GetInstructionStack(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, GetInstructionStack)

    def test_list_instructions(self) -> None:
        from tstd.protocol import ListInstructions

        msg = ListInstructions(workspace_path="/home/user/project")
        back = _roundtrip(msg)
        assert isinstance(back, ListInstructions)
        assert back.workspace_path == "/home/user/project"

    def test_list_memory(self) -> None:
        from tstd.protocol import ListMemory

        msg = ListMemory(workspace_path="/home/user/project")
        back = _roundtrip(msg)
        assert isinstance(back, ListMemory)
        assert back.workspace_path == "/home/user/project"

    def test_save_memory(self) -> None:
        from tstd.protocol import SaveMemory

        msg = SaveMemory(
            workspace_path="/home/user/project",
            path="MEMORY.md",
            content="durable: ruff\n",
        )
        back = _roundtrip(msg)
        assert isinstance(back, SaveMemory)
        assert back.path == "MEMORY.md"
        assert back.content == "durable: ruff\n"

    def test_create_rule(self) -> None:
        from tstd.protocol import CreateRule

        msg = CreateRule(workspace_path="/home/user/project", name="api")
        back = _roundtrip(msg)
        assert isinstance(back, CreateRule)
        assert back.name == "api"

    def test_get_charter(self) -> None:
        from tstd.protocol import GetCharter

        msg = GetCharter(workspace_path="/home/user/project")
        back = _roundtrip(msg)
        assert isinstance(back, GetCharter)
        assert back.workspace_path == "/home/user/project"

    def test_save_charter(self) -> None:
        from tstd.protocol import SaveCharter

        msg = SaveCharter(
            workspace_path="/home/user/project",
            charter={"objective": "x", "definition_of_done": ["done"]},
            notes="pane notes",
        )
        back = _roundtrip(msg)
        assert isinstance(back, SaveCharter)
        assert back.charter["objective"] == "x"
        assert back.notes == "pane notes"

    def test_start_autonomy(self) -> None:
        from tstd.protocol import StartAutonomy

        msg = StartAutonomy(
            workspace_path="/home/user/project",
            charter={"objective": "x", "definition_of_done": ["done"]},
            notes="sign",
        )
        back = _roundtrip(msg)
        assert isinstance(back, StartAutonomy)
        assert back.charter is not None
        assert back.charter["objective"] == "x"
        assert back.notes == "sign"

    def test_autonomy_start(self) -> None:
        from tstd.protocol import AutonomyStart

        ev = AutonomyStart(
            workspace_path="/home/user/project",
            ready=False,
            signed=True,
            error="Install Podman",
        )
        back = _roundtrip(ev)
        assert isinstance(back, AutonomyStart)
        assert back.ready is False
        assert back.signed is True
        assert back.error == "Install Podman"
        assert back.session_id is None
        ready = AutonomyStart(
            workspace_path="/home/user/project",
            ready=True,
            signed=True,
            session_id="sess-autonomy",
        )
        assert _roundtrip(ready).session_id == "sess-autonomy"

    def test_charter_document(self) -> None:
        from tstd.autonomy.charter import Charter
        from tstd.protocol import CharterDocument

        charter = Charter.model_validate(
            {
                "objective": "x",
                "definition_of_done": ["done"],
                "boundary": {"network": "deny"},
                "caps": {"spend_usd": 1},
            }
        )
        msg = CharterDocument(
            workspace_path="/home/user/project",
            present=True,
            charter=charter,
            notes="context",
        )
        back = _roundtrip(msg)
        assert isinstance(back, CharterDocument)
        assert back.present is True
        assert back.charter is not None
        assert back.charter.objective == "x"
        assert back.notes == "context"

    def test_list_pins(self) -> None:
        from tstd.protocol import ListPins

        back = _roundtrip(ListPins(workspace_path="/home/user/project"))
        assert isinstance(back, ListPins)

    def test_add_pin(self) -> None:
        from tstd.protocol import AddPin

        back = _roundtrip(AddPin(workspace_path="/home/user/project", path="src/app.ts"))
        assert isinstance(back, AddPin)
        assert back.path == "src/app.ts"

    def test_remove_pin(self) -> None:
        from tstd.protocol import RemovePin

        back = _roundtrip(RemovePin(workspace_path="/home/user/project", path="src/app.ts"))
        assert isinstance(back, RemovePin)

    def test_memory_accept(self) -> None:
        back = _roundtrip(MemoryAccept(session_id="sess-1", proposal_id="mp-1"))
        assert isinstance(back, MemoryAccept)
        assert back.proposal_id == "mp-1"

    def test_memory_edit(self) -> None:
        back = _roundtrip(
            MemoryEdit(
                session_id="sess-1",
                proposal_id="mp-1",
                files=[MemoryFileEdit(path=".tst/memory/MEMORY.md", content="x\n")],
            )
        )
        assert isinstance(back, MemoryEdit)
        assert back.files[0].content == "x\n"

    def test_memory_reject(self) -> None:
        back = _roundtrip(MemoryReject(session_id="sess-1", proposal_id="mp-1"))
        assert isinstance(back, MemoryReject)
        assert back.proposal_id == "mp-1"

    def test_end_session(self) -> None:
        back = _roundtrip(EndSession(session_id="sess-1"))
        assert isinstance(back, EndSession)
        assert back.session_id == "sess-1"

    def test_new_session(self) -> None:
        msg = NewSession(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, NewSession)
        assert back.session_id == "sess-1"

    # ── Session lifecycle (TD-1715) ─────────────────────────────────
    #
    # These go through `_roundtrip`, which calls `parse_client_message` —
    # so each one also proves the type is in `_KNOWN_CLIENT_TYPES`. A
    # message added to the union alone parses as "unknown_message", which
    # is the failure mode this trio is most likely to hit.

    def test_archive_session(self) -> None:
        msg = ArchiveSession(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, ArchiveSession)
        assert back.session_id == "sess-1"
        # Archiving is the common case, so it is the default.
        assert back.archived is True

    def test_archive_session_restores_too(self) -> None:
        back = _roundtrip(ArchiveSession(session_id="sess-1", archived=False))
        assert isinstance(back, ArchiveSession)
        assert back.archived is False

    def test_set_session_star(self) -> None:
        back = _roundtrip(SetSessionStar(session_id="sess-1"))
        assert isinstance(back, SetSessionStar)
        assert back.starred is True

    def test_set_session_star_clears(self) -> None:
        back = _roundtrip(SetSessionStar(session_id="sess-1", starred=False))
        assert isinstance(back, SetSessionStar)
        assert back.starred is False

    def test_delete_session(self) -> None:
        back = _roundtrip(DeleteSession(session_id="sess-1"))
        assert isinstance(back, DeleteSession)
        assert back.session_id == "sess-1"

    def test_move_session(self) -> None:
        back = _roundtrip(MoveSession(session_id="sess-1", workspace_path="/ws/other"))
        assert isinstance(back, MoveSession)
        assert back.workspace_path == "/ws/other"

    def test_move_session_rejects_an_empty_target(self) -> None:
        with pytest.raises(ValidationError):
            MoveSession(session_id="sess-1", workspace_path="")

    def test_rename_session(self) -> None:
        back = _roundtrip(RenameSession(session_id="sess-1", title="My name"))
        assert isinstance(back, RenameSession)
        assert back.session_id == "sess-1"
        assert back.title == "My name"

    def test_rename_session_empty_means_restore(self) -> None:
        back = _roundtrip(RenameSession(session_id="sess-1", title=""))
        assert isinstance(back, RenameSession)
        assert back.title == ""

    # ── Usage and cost (TD-1706) ────────────────────────────────────
    #
    # `_roundtrip` parses through `parse_client_message`, so these also
    # prove both types reached `_KNOWN_CLIENT_TYPES` and not just the
    # union — a message added to one alone parses as "unknown_message".

    def test_get_usage(self) -> None:
        back = _roundtrip(GetUsage())
        assert isinstance(back, GetUsage)

    def test_export_usage_defaults_to_jsonl(self) -> None:
        back = _roundtrip(ExportUsage())
        assert isinstance(back, ExportUsage)
        assert back.format == "jsonl"

    def test_export_usage_takes_csv(self) -> None:
        back = _roundtrip(ExportUsage(format="csv"))
        assert isinstance(back, ExportUsage)
        assert back.format == "csv"

    def test_export_usage_rejects_an_unknown_format(self) -> None:
        with pytest.raises(ValidationError):
            ExportUsage(format="parquet")  # type: ignore[arg-type]


# ── Daemon → Client ────────────────────────────────────────────────────


class TestDaemonEvents:
    def test_ready(self) -> None:
        evt = Ready(version="0.1.0", protocol_version=PROTOCOL_VERSION)
        back = _roundtrip(evt)
        assert isinstance(back, Ready)
        assert back.version == "0.1.0"

    def test_session_state(self) -> None:
        evt = SessionState(session_id="sess-1", state="running", seq=2)
        back = _roundtrip(evt)
        assert isinstance(back, SessionState)
        assert back.state == "running"
        for s in ("idle", "running", "awaiting_approval", "complete", "failed", "cancelled"):
            m = SessionState(session_id="sess-1", state=s, seq=3)
            r = _roundtrip(m)
            assert isinstance(r, SessionState)
            assert r.state == s

    def test_assistant_delta(self) -> None:
        evt = AssistantDelta(session_id="sess-1", delta="Hello ", seq=1)
        back = _roundtrip(evt)
        assert isinstance(back, AssistantDelta)
        assert back.delta == "Hello "

    def test_assistant_reasoning(self) -> None:
        evt = AssistantReasoning(session_id="sess-1", delta="Let me think", seq=1)
        back = _roundtrip(evt)
        assert isinstance(back, AssistantReasoning)
        assert back.delta == "Let me think"

    def test_tool_call(self) -> None:
        evt = ToolCall(
            session_id="sess-1",
            tool_call_id="tc-1",
            name="fs_read",
            arguments={"path": "/tmp/test.txt"},
            seq=3,
            decision_class="B",
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolCall)
        assert back.name == "fs_read"
        assert back.arguments == {"path": "/tmp/test.txt"}
        assert back.decision_class == "B"

    def test_tool_call_no_decision_class(self) -> None:
        evt = ToolCall(
            session_id="sess-1",
            tool_call_id="tc-1",
            name="fs_read",
            arguments={},
            seq=3,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolCall)
        assert back.decision_class is None

    def test_tool_result(self) -> None:
        evt = ToolResult(
            session_id="sess-1",
            tool_call_id="tc-1",
            status="success",
            output="file contents",
            seq=4,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolResult)
        assert back.status == "success"
        assert back.output == "file contents"
        assert back.truncated is False

    def test_tool_result_truncated(self) -> None:
        evt = ToolResult(
            session_id="sess-1",
            tool_call_id="tc-1",
            status="error",
            output="too long",
            seq=4,
            truncated=True,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolResult)
        assert back.truncated is True

    def test_shell_output(self) -> None:
        evt = ShellOutput(
            session_id="sess-1",
            tool_call_id="tc-1",
            stream="stdout",
            chunk="hello\n",
            seq=13,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ShellOutput)
        assert back.tool_call_id == "tc-1"
        assert back.stream == "stdout"
        assert back.chunk == "hello\n"
        assert back.seq == 13

    def test_shell_output_stderr(self) -> None:
        evt = ShellOutput(
            session_id="sess-1",
            tool_call_id="tc-1",
            stream="stderr",
            chunk="boom",
            seq=14,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ShellOutput)
        assert back.stream == "stderr"

    def test_shell_output_invalid_stream(self) -> None:
        with pytest.raises(ValidationError):
            ShellOutput(
                session_id="sess-1",
                tool_call_id="tc-1",
                stream="stdin",  # type: ignore[arg-type]
                chunk="x",
                seq=15,
            )

    def test_approval_request(self) -> None:
        evt = ApprovalRequest(
            session_id="sess-1",
            tool_call_id="tc-1",
            tool_name="fs_write",
            arguments={"path": "/tmp/test.txt"},
            decision_class="C",
            summary="Write to /tmp/test.txt",
            reason="decision class C requires approval",
            seq=5,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ApprovalRequest)
        assert back.tool_name == "fs_write"
        assert back.decision_class == "C"
        assert back.summary == "Write to /tmp/test.txt"

    def test_approval_request_proposal(self) -> None:
        evt = ApprovalRequest(
            session_id="sess-1",
            tool_call_id="tc-2",
            tool_name="shell",
            arguments={"command": "npm test"},
            decision_class="B",
            summary="Run `npm test`",
            reason="decision class B requires approval",
            proposed_always_allow=PolicyRuleSummary(tool="shell", args="npm test", effect="auto"),
            seq=6,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ApprovalRequest)
        assert back.proposed_always_allow is not None
        assert back.proposed_always_allow.tool == "shell"
        assert back.proposed_always_allow.args == "npm test"
        assert back.proposed_always_allow.effect == "auto"

    def test_approval_request_no_proposal_for_class_c(self) -> None:
        evt = ApprovalRequest(
            session_id="sess-1",
            tool_call_id="tc-3",
            tool_name="fs_write",
            arguments={"path": "/tmp/test.txt"},
            decision_class="C",
            summary="Write to /tmp/test.txt",
            reason="decision class C requires approval",
            seq=7,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ApprovalRequest)
        assert back.proposed_always_allow is None

    def test_policy_rules(self) -> None:
        evt = PolicyRules(
            seq=1,
            rules=[PolicyRuleSummary(tool="shell", args="npm test", effect="auto")],
        )
        back = _roundtrip(evt)
        assert isinstance(back, PolicyRules)
        assert back.rules == [PolicyRuleSummary(tool="shell", args="npm test", effect="auto")]

    def test_memory_files(self) -> None:
        from tstd.protocol import MemoryFileEntry, MemoryFiles

        evt = MemoryFiles(
            workspace_path="/home/user/project",
            files=[
                MemoryFileEntry(
                    path="/home/user/project/.tst/memory/MEMORY.md",
                    name="MEMORY.md",
                    content="durable: ruff\n",
                )
            ],
        )
        back = _roundtrip(evt)
        assert isinstance(back, MemoryFiles)
        assert back.files[0].name == "MEMORY.md"

    def test_memory_proposal(self) -> None:
        from tstd.protocol import MemoryFileDiff

        evt = MemoryProposal(
            session_id="sess-1",
            proposal_id="mp-1",
            files=[
                MemoryFileDiff(
                    action="replace",
                    path=".tst/memory/MEMORY.md",
                    diff="--- a\n+++ b\n",
                    before="old\n",
                    after="new\n",
                )
            ],
            seq=21,
        )
        back = _roundtrip(evt)
        assert isinstance(back, MemoryProposal)
        assert back.proposal_id == "mp-1"
        assert back.files[0].action == "replace"

    def test_decision_logged(self) -> None:
        evt = DecisionLogged(
            session_id="sess-1",
            decision_class="A",
            what="Formatted file",
            why="Ruff format",
            commit="abc123",
            seq=6,
        )
        back = _roundtrip(evt)
        assert isinstance(back, DecisionLogged)
        assert back.decision_class == "A"
        assert back.commit == "abc123"

    def test_cost_update(self) -> None:
        evt = CostUpdate(
            session_id="sess-1",
            turn_cost=0.05,
            session_cost=0.50,
            total_cost=1.20,
            seq=7,
        )
        back = _roundtrip(evt)
        assert isinstance(back, CostUpdate)
        assert back.turn_cost == 0.05
        assert back.total_cost == 1.20

    def test_turn_complete(self) -> None:
        evt = TurnComplete(
            session_id="sess-1",
            tokens=1500,
            cost=0.03,
            tier="worker",
            duration=2.5,
            seq=8,
        )
        back = _roundtrip(evt)
        assert isinstance(back, TurnComplete)
        assert back.tokens == 1500
        assert back.tier == "worker"
        assert back.duration == 2.5

    def test_error(self) -> None:
        evt = Error(code="test", message="something went wrong", seq=9)
        back = _roundtrip(evt)
        assert isinstance(back, Error)
        assert back.code == "test"
        assert back.session_id is None

    def test_error_with_session(self) -> None:
        evt = Error(session_id="sess-1", code="test", message="fail", seq=10)
        assert isinstance(evt, Error)
        assert evt.session_id == "sess-1"

    # ── Usage and cost (TD-1706) ────────────────────────────────────

    def test_usage_report(self) -> None:
        evt = UsageReport(
            rows=[
                UsageRollup(
                    bucket="day",
                    key="2026-08-17",
                    tier="brain",
                    prompt_tokens=40_000,
                    cached_prompt_tokens=10_000,
                    completion_tokens=5_000,
                    cost=0.165,
                    classifier_cost=0.0008,
                )
            ]
        )
        back = _roundtrip(evt)
        assert isinstance(back, UsageReport)
        assert back.seq == 1  # connection-scoped, like diagnostics_report
        assert back.rows[0].bucket == "day"
        assert back.rows[0].cost == pytest.approx(0.165)
        # Classifier spend rides its own field, never folded into cost.
        assert back.rows[0].classifier_cost == pytest.approx(0.0008)

    def test_usage_report_defaults_to_no_rows(self) -> None:
        back = _roundtrip(UsageReport())
        assert isinstance(back, UsageReport)
        assert back.rows == []

    def test_usage_rollup_rejects_negative_money(self) -> None:
        with pytest.raises(ValidationError):
            UsageRollup(
                bucket="day",
                key="2026-08-17",
                tier="brain",
                prompt_tokens=1,
                cached_prompt_tokens=0,
                completion_tokens=0,
                cost=-0.01,
            )

    def test_usage_exported(self) -> None:
        evt = UsageExported(format="csv", path="/data/exports/usage.csv", rows=42)
        back = _roundtrip(evt)
        assert isinstance(back, UsageExported)
        assert back.format == "csv"
        assert back.path == "/data/exports/usage.csv"
        assert back.rows == 42

    def test_cu_kill_state(self) -> None:
        evt = CuKillState(killed=True)
        back = _roundtrip(evt)
        assert isinstance(back, CuKillState)
        assert back.killed is True
        assert back.seq == 1
        assert "session_id" not in CuKillState.model_fields

    def test_cu_session(self) -> None:
        evt = CuSession(session_id="sess-1", active=True)
        back = _roundtrip(evt)
        assert isinstance(back, CuSession)
        assert back.active is True
        assert back.session_id == "sess-1"


# ── Discriminated union dispatch ───────────────────────────────────────


class TestDiscriminatedUnion:
    def test_client_message_dispatch(self) -> None:
        cancel = parse_client_message('{"type": "cancel", "session_id": "sess-1"}')
        assert isinstance(cancel, Cancel)
        approve = parse_client_message(
            '{"type": "approve", "session_id": "sess-1", "tool_call_id": "tc-1"}',
        )
        assert isinstance(approve, Approve)

    def test_daemon_event_dispatch(self) -> None:
        ready = parse_daemon_event(
            '{"type": "ready", "version": "0.1.0", "protocol_version": 1, "seq": 1}',
        )
        assert isinstance(ready, Ready)
        error = parse_daemon_event('{"type": "error", "code": "test", "message": "x", "seq": 2}')
        assert isinstance(error, Error)

    def test_unknown_client_message_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError) as exc:
            parse_client_message('{"type": "bogus_type", "session_id": "sess-1"}')
        assert "bogus_type" in str(exc)
        assert exc.value.code == "unknown_message"

    def test_unknown_daemon_event_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError) as exc:
            parse_daemon_event('{"type": "bogus_event", "seq": 1}')
        assert "bogus_event" in str(exc)


# ── The known-type gate ────────────────────────────────────────────────


def _wire_types(union: Any) -> dict[str, type[BaseModel]]:
    """Every model in a discriminated union, keyed by its wire ``type``.

    Read off the annotation rather than restated: the union is the
    contract, so it is what the frozensets are judged against.
    """
    return {
        get_args(model.model_fields["type"].annotation)[0]: model
        for model in get_args(get_args(union)[0])
    }


CLIENT_MESSAGES = _wire_types(ClientMessageT)
DAEMON_EVENTS = _wire_types(DaemonEventT)


def _placeholder(annotation: Any) -> Any:
    """The smallest value satisfying ``annotation`` and the field
    constraints the protocol uses (``gt=0``, ``ge=1``, ``min_length=1``).

    Deliberately narrow.  A message introducing a shape this cannot build
    fails loudly and costs one line here, which is the price of never
    hand-listing the samples — a hand-listed set is what let TD-208 hide.
    """
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, UnionType):
        return _placeholder(get_args(annotation)[0])
    if origin is list:
        return []
    if origin is dict:
        return {}
    if annotation is bool:
        return False
    if annotation is str:
        return "x"
    if annotation in (int, float):
        return 1
    raise AssertionError(f"no placeholder for {annotation!r} — extend _placeholder")


def _sample(model: type[BaseModel]) -> BaseModel:
    """A minimal valid instance of ``model``: its required fields only."""
    return model(
        **{
            name: _placeholder(field.annotation)
            for name, field in model.model_fields.items()
            if field.is_required()
        }
    )


class TestKnownTypeGate:
    """`_check_known_type` runs before validation, so the hand-maintained
    frozensets are part of the parse contract, not a convenience.

    A union member missing from its frozenset is refused by the parser
    meant to accept it; a frozenset entry with no union member advertises
    a type nothing can validate. TD-208 was the first case — the daemon
    emitted `rule_activated` and its own parser rejected it. Both sets are
    derived from the unions here and compared in both directions, so the
    next omission fails the suite rather than shipping.
    """

    def test_known_event_types_match_the_union(self) -> None:
        declared = set(DAEMON_EVENTS)
        assert declared == _KNOWN_EVENT_TYPES, (
            f"in DaemonEventT, missing from _KNOWN_EVENT_TYPES: "
            f"{sorted(declared - _KNOWN_EVENT_TYPES)}; "
            f"in _KNOWN_EVENT_TYPES, missing from DaemonEventT: "
            f"{sorted(_KNOWN_EVENT_TYPES - declared)}"
        )

    def test_known_client_types_match_the_union(self) -> None:
        declared = set(CLIENT_MESSAGES)
        assert declared == _KNOWN_CLIENT_TYPES, (
            f"in ClientMessageT, missing from _KNOWN_CLIENT_TYPES: "
            f"{sorted(declared - _KNOWN_CLIENT_TYPES)}; "
            f"in _KNOWN_CLIENT_TYPES, missing from ClientMessageT: "
            f"{sorted(_KNOWN_CLIENT_TYPES - declared)}"
        )

    # The parsers are called directly rather than through ``_roundtrip``:
    # that helper picks one by ``isinstance(msg, DaemonEvent)``, and
    # ``ping`` is a member of ``DaemonEventT`` without being a
    # ``DaemonEvent`` — it carries no seq.
    @pytest.mark.parametrize("wire_type", sorted(DAEMON_EVENTS))
    def test_every_daemon_event_round_trips(self, wire_type: str) -> None:
        model = DAEMON_EVENTS[wire_type]
        back = parse_daemon_event(_sample(model).model_dump_json())
        assert type(back) is model

    @pytest.mark.parametrize("wire_type", sorted(CLIENT_MESSAGES))
    def test_every_client_message_round_trips(self, wire_type: str) -> None:
        model = CLIENT_MESSAGES[wire_type]
        back = parse_client_message(_sample(model).model_dump_json())
        assert type(back) is model


# ── Edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_client_message_missing_type(self) -> None:
        with pytest.raises(HandshakeError):
            parse_client_message('{"session_id": "sess-1"}')

    def test_client_message_not_json(self) -> None:
        with pytest.raises(HandshakeError):
            parse_client_message("not json")

    def test_daemon_event_missing_type(self) -> None:
        with pytest.raises(HandshakeError):
            parse_daemon_event('{"seq": 1}')

    def test_daemon_event_missing_seq(self) -> None:
        with pytest.raises(HandshakeError):
            parse_daemon_event(
                '{"type": "session_state", "session_id": "sess-1", "state": "running"}',
            )

    def test_build_hello_ack(self) -> None:
        msg = json.loads(build_hello_ack())
        assert msg["type"] == "hello_ack"
        assert msg["version"] == PROTOCOL_VERSION

    def test_build_error(self) -> None:
        msg = json.loads(build_error("err", "msg"))
        assert msg["code"] == "err"
        assert msg["message"] == "msg"

    def test_hello_parse_roundtrip(self) -> None:
        hello = parse_hello('{"type": "hello", "token": "x", "version": 1}')
        assert hello.token == "x"
        assert hello.version == 1
