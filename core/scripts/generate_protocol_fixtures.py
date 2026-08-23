"""Generate JSON fixtures for all protocol message types.

Used by the TypeScript cross-language test to verify type definitions match.
Writes a JSON file with one sample per message type.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add core to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.protocol import (
    PROTOCOL_VERSION,
    AddPin,
    AlwaysAllow,
    ApiKeyValidated,
    ApprovalRequest,
    Approve,
    ArchiveSession,
    Artifact,
    ArtifactEntry,
    ArtifactList,
    ArtifactReady,
    AssistantDelta,
    AssistantReasoning,
    Attach,
    Attachment,
    AutonomyStart,
    BoundaryUpdate,
    Cancel,
    CharterDocument,
    CheckCuPermissions,
    CheckpointNotice,
    ContextCompacted,
    ContextPinEntry,
    ContextPins,
    ConversationReset,
    CostUpdate,
    CreateRule,
    CredentialSummary,
    CuKillState,
    CuPermissions,
    DecisionLogged,
    DeleteApiKey,
    DeleteCredential,
    DeleteJob,
    DeleteSession,
    Deny,
    DesignHit,
    DesignHitBox,
    DesignHitTest,
    Detach,
    DiagnosticCheck,
    DiagnosticsReport,
    EndSession,
    Error,
    ExportUsage,
    ForkFrom,
    GetCharter,
    GetInstructionStack,
    GetSetupState,
    GetUsage,
    Hello,
    InstructionFileEntry,
    InstructionFiles,
    InstructionStack,
    JobEntry,
    JobList,
    ListArtifacts,
    ListInstructions,
    ListJobs,
    ListMemory,
    ListPins,
    ListPolicyRules,
    ListSessions,
    LogTrimmed,
    MemoryAccept,
    MemoryEdit,
    MemoryFileDiff,
    MemoryFileEdit,
    MemoryFileEntry,
    MemoryFiles,
    MemoryProposal,
    MemoryReject,
    MoveSession,
    OpenArtifact,
    OpenWorkspace,
    Ping,
    PolicyRules,
    PolicyRuleSummary,
    Ready,
    RemovePin,
    RenameSession,
    Resume,
    RevokePolicyRule,
    RuleActivated,
    RunDiagnostics,
    SaveCharter,
    SaveJob,
    SaveMemory,
    ScreenFrame,
    SessionList,
    SessionState,
    SetApiKey,
    SetBranch,
    SetCoworker,
    SetCredential,
    SetCuIndicators,
    SetCuKill,
    SetLoadGlobalMemory,
    SetPreset,
    SetRemoteAttach,
    SetSessionStar,
    SetSkipAllApprovals,
    SetTier,
    SetTierCredential,
    SetupState,
    SetWorkspacePin,
    ShellOutput,
    Shutdown,
    StartAutonomy,
    SteeringReloaded,
    TierState,
    TierSwitched,
    ToolCall,
    ToolResult,
    TurnComplete,
    UsageExported,
    UsageReport,
    UsageRollup,
    UserMessage,
    UserTurn,
    ValidateApiKey,
)

FIXTURES = {
    # Client messages
    "hello": Hello(token="test-token-abc", version=1),
    "open_workspace": OpenWorkspace(path="/home/user/project"),
    "user_message": UserMessage(session_id="sess-1", content="hello world"),
    "fork_from": ForkFrom(session_id="sess-1", user_index=0, content="hello again"),
    "set_branch": SetBranch(session_id="sess-1", user_index=0, sibling_index=1),
    # TD-1709: additive field on an existing message, so the plain fixture
    # above is also the proof that a client sending none behaves as it did.
    # base64 is the payload because the daemon, not the client, decides
    # whether the bytes are text.
    "user_message_attachments": UserMessage(
        session_id="sess-1",
        content="what does this do?",
        attachments=[
            Attachment(name="notes.md", content_b64="IyBUaXRsZQo="),
            Attachment(name="empty.txt"),
        ],
    ),
    "approve": Approve(session_id="sess-1", tool_call_id="tc-1"),
    "deny": Deny(session_id="sess-1", tool_call_id="tc-1", reason="not safe"),
    "deny_no_reason": Deny(session_id="sess-1", tool_call_id="tc-1"),
    "always_allow": AlwaysAllow(session_id="sess-1", tool_call_id="tc-1"),
    "list_policy_rules": ListPolicyRules(session_id="sess-1"),
    "revoke_policy_rule": RevokePolicyRule(session_id="sess-1", tool="shell", args="rm *"),
    "set_skip_all_approvals": SetSkipAllApprovals(enabled=True),
    "set_load_global_memory": SetLoadGlobalMemory(enabled=True),
    "set_coworker": SetCoworker(enabled=True),
    "set_cu_indicators": SetCuIndicators(glow=True, agent_cursor=True, show_on_real_display=False),
    "set_workspace_pin": SetWorkspacePin(path="/home/user/project", pinned=True),
    "resume": Resume(session_id="sess-1"),
    "cancel": Cancel(session_id="sess-1"),
    "attach": Attach(session_id="sess-1", from_seq=5),
    "detach": Detach(session_id="sess-1"),
    "set_tier": SetTier(session_id="sess-1", tier="brain"),
    "get_instruction_stack": GetInstructionStack(session_id="sess-1"),
    "list_instructions": ListInstructions(workspace_path="/home/user/project"),
    "list_memory": ListMemory(workspace_path="/home/user/project"),
    "save_memory": SaveMemory(
        workspace_path="/home/user/project",
        path="MEMORY.md",
        content="durable: ruff\n",
    ),
    "create_rule": CreateRule(workspace_path="/home/user/project", name="api"),
    "get_charter": GetCharter(workspace_path="/home/user/project"),
    "save_charter": SaveCharter(
        workspace_path="/home/user/project",
        charter={
            "objective": "Ship the CSV importer end to end.",
            "definition_of_done": ["cargo test passes"],
            "source_of_truth": ["docs/spec.md"],
            "boundary": {
                "writable_paths": ["src/**", "tests/**"],
                "allowed_commands": ["cargo"],
                "network": "deny",
            },
            "caps": {
                "spend_usd": 25.0,
                "wall_clock_hours": 8.0,
                "max_iterations": 200,
            },
            "stop_conditions": ["any Class C decision"],
        },
        notes="Human context.",
    ),
    "start_autonomy": StartAutonomy(
        workspace_path="/home/user/project",
        charter={
            "objective": "Ship the CSV importer end to end.",
            "definition_of_done": ["cargo test passes"],
            "source_of_truth": ["docs/spec.md"],
            "boundary": {
                "writable_paths": ["src/**", "tests/**"],
                "allowed_commands": ["cargo"],
                "network": "deny",
            },
            "caps": {
                "spend_usd": 25.0,
                "wall_clock_hours": 8.0,
                "max_iterations": 200,
            },
            "stop_conditions": ["any Class C decision"],
        },
        notes="Human context.",
    ),
    "list_pins": ListPins(workspace_path="/home/user/project"),
    "add_pin": AddPin(workspace_path="/home/user/project", path="src/app.ts"),
    "remove_pin": RemovePin(workspace_path="/home/user/project", path="src/app.ts"),
    "memory_accept": MemoryAccept(session_id="sess-1", proposal_id="mp-1"),
    "memory_edit": MemoryEdit(
        session_id="sess-1",
        proposal_id="mp-1",
        files=[MemoryFileEdit(path=".tst/memory/MEMORY.md", content="durable: ruff\n")],
    ),
    "memory_reject": MemoryReject(session_id="sess-1", proposal_id="mp-1"),
    "end_session": EndSession(session_id="sess-1"),
    "shutdown": Shutdown(),
    "list_sessions": ListSessions(),
    # Session lifecycle (TD-1715): archive/restore, delete, move to project.
    "archive_session": ArchiveSession(session_id="sess-1"),
    "unarchive_session": ArchiveSession(session_id="sess-1", archived=False),
    "set_session_star": SetSessionStar(session_id="sess-1"),
    "unstar_session": SetSessionStar(session_id="sess-1", starred=False),
    "delete_session": DeleteSession(session_id="sess-1"),
    "move_session": MoveSession(session_id="sess-1", workspace_path="/home/user/other"),
    "rename_session": RenameSession(session_id="sess-1", title="My name"),
    "rename_session_restore": RenameSession(session_id="sess-1", title=""),
    # Onboarding (TD-1101 first-run wizard)
    "get_setup_state": GetSetupState(),
    "set_api_key": SetApiKey(api_key="sk-or-test-key"),
    "delete_api_key": DeleteApiKey(),
    "validate_api_key": ValidateApiKey(),
    "set_preset": SetPreset(name="tst-default"),
    "set_credential": SetCredential(name="Local"),
    "delete_credential": DeleteCredential(credential="local"),
    "set_tier_credential": SetTierCredential(preset="local", tier="brain", credential="local"),
    # Diagnostics (TD-1104 doctor)
    "run_diagnostics": RunDiagnostics(),
    # Usage and cost (TD-1706)
    "get_usage": GetUsage(),
    "export_usage": ExportUsage(format="csv"),
    # Artifacts (TD-3201): list/open on the wire; record is a daemon API.
    "list_artifacts": ListArtifacts(session_id="sess-1"),
    "open_artifact": OpenArtifact(session_id="sess-1", artifact_id="art-1"),
    "design_hit_test": DesignHitTest(session_id="sess-1", x=12.0, y=34.0),
    "check_cu_permissions": CheckCuPermissions(),
    "set_cu_kill": SetCuKill(killed=True),
    "set_remote_attach": SetRemoteAttach(enabled=True),
    # Daemon events
    "ready": Ready(version="0.1.0", protocol_version=PROTOCOL_VERSION),
    "session_state": SessionState(session_id="sess-1", state="running", seq=2),
    "conversation_reset": ConversationReset(
        session_id="sess-1",
        user_index=0,
        sibling_index=1,
        sibling_count=2,
        content="hello again",
        seq=2,
    ),
    "session_state_paused": SessionState(
        session_id="sess-1",
        state="paused",
        reason="spend cap exceeded: $0.0205 >= $0.01",
        seq=2,
    ),
    "user_turn": UserTurn(
        session_id="sess-1",
        turn_id="turn-1",
        content="Fix the tests",
        seq=3,
    ),
    "assistant_delta": AssistantDelta(session_id="sess-1", delta="Hello ", seq=3),
    "assistant_reasoning": AssistantReasoning(session_id="sess-1", delta="Let me think", seq=3),
    "tool_call": ToolCall(
        session_id="sess-1",
        tool_call_id="tc-1",
        name="fs_read",
        arguments={"path": "test.txt"},
        seq=4,
        decision_class="B",
    ),
    "tool_result": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="success",
        output="file content",
        seq=5,
    ),
    "tool_result_truncated": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="error",
        output="too long",
        seq=6,
        truncated=True,
    ),
    "tool_result_diff": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="success",
        output="wrote 12 bytes; overwrote test.txt",
        seq=7,
        diff="--- a/test.txt\n+++ b/test.txt\n@@ -1 +1 @@\n-old line\n+new line",
    ),
    "tool_result_denied": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="error",
        output="Denied by user: not safe",
        seq=20,
        error_code="approval_denied",
    ),
    "approval_request": ApprovalRequest(
        session_id="sess-1",
        tool_call_id="tc-1",
        tool_name="fs_write",
        arguments={"path": "test.txt"},
        decision_class="C",
        summary="Write to test.txt",
        reason="decision class C requires approval",
        seq=8,
    ),
    "approval_request_always_allow": ApprovalRequest(
        session_id="sess-1",
        tool_call_id="tc-2",
        tool_name="shell",
        arguments={"command": "npm test"},
        decision_class="B",
        summary="Run `npm test`",
        reason="decision class B requires approval",
        proposed_always_allow=PolicyRuleSummary(tool="shell", args="npm test", effect="auto"),
        seq=9,
    ),
    "decision_logged": DecisionLogged(
        session_id="sess-1",
        decision_class="A",
        what="Formatted file",
        why="Ruff format",
        commit="abc123",
        seq=9,
    ),
    "checkpoint_notice": CheckpointNotice(
        session_id="sess-1",
        code="dirty_baseline",
        message="This workspace has uncommitted changes.",
        seq=10,
    ),
    "cost_update": CostUpdate(
        session_id="sess-1",
        turn_cost=0.05,
        session_cost=0.50,
        total_cost=1.20,
        classifier_cost=0.01,
        cost_by_tier={"brain": 0.40, "worker": 0.10},
        seq=11,
    ),
    "boundary_update": BoundaryUpdate(
        session_id="sess-1",
        writable_paths=["src/**", "tests/**"],
        allowed_commands=["cargo", "git"],
        network="deny",
        spend_usd=25.0,
        wall_clock_hours=8.0,
        max_iterations=200,
        source="defaults",
        seq=12,
    ),
    "turn_complete": TurnComplete(
        session_id="sess-1",
        tokens=1500,
        cost=0.03,
        tier="worker",
        duration=2.5,
        seq=12,
    ),
    "turn_complete_failed": TurnComplete(
        session_id="sess-1",
        tokens=0,
        cost=0.0,
        tier="brain",
        duration=0.4,
        failed=True,
        error_code="auth_failed",
        seq=13,
    ),
    "tier_state": TierState(
        session_id="sess-1",
        tier="brain",
        override=None,
        model_slugs={
            "brain": "test-brain-slug",
            "worker": "test-worker-slug",
            "validator": "test-validator-slug",
        },
        seq=18,
    ),
    "tier_state_override": TierState(
        session_id="sess-1",
        tier="validator",
        override="validator",
        model_slugs={
            "brain": "test-brain-slug",
            "worker": "test-worker-slug",
            "validator": "test-validator-slug",
        },
        seq=19,
    ),
    # TD-1716 liveness frame: no session, no seq — deliberately unlike
    # every other daemon→client frame, which is the point of the fixture.
    "ping": Ping(),
    "error": Error(code="test", message="fail", seq=13),
    "error_with_session": Error(session_id="sess-1", code="test", message="fail", seq=14),
    "shell_output": ShellOutput(
        session_id="sess-1",
        tool_call_id="tc-1",
        stream="stdout",
        chunk="hello\n",
        seq=15,
    ),
    "steering_reloaded": SteeringReloaded(
        session_id="sess-1",
        prefix_hash="abc123",
        prefix_tokens=100,
        steering_tokens=60,
        source_count=3,
        seq=16,
    ),
    "rule_activated": RuleActivated(
        session_id="sess-1",
        rule_path=".tst/rules/api-rules.md",
        seq=16,
    ),
    "instruction_stack": InstructionStack(
        session_id="sess-1",
        sources=[
            {
                "path": "CLAUDE.md",
                "precedence": "project",
                "tokens": 500,
                "token_method": "cl100k_base",
            }
        ],
        total_tokens=500,
        token_method="cl100k_base",
        seq=17,
    ),
    "charter": CharterDocument(
        workspace_path="/home/user/project",
        present=True,
        charter={
            "objective": "Ship the CSV importer end to end.",
            "definition_of_done": ["cargo test passes"],
            "source_of_truth": ["docs/spec.md"],
            "boundary": {
                "writable_paths": ["src/**", "tests/**"],
                "allowed_commands": ["cargo"],
                "network": "deny",
            },
            "caps": {
                "spend_usd": 25.0,
                "wall_clock_hours": 8.0,
                "max_iterations": 200,
            },
            "stop_conditions": ["any Class C decision"],
        },
        notes="Human context.",
    ),
    "autonomy_start": AutonomyStart(
        workspace_path="/home/user/project",
        ready=True,
        signed=True,
        error=None,
        session_id="sess-autonomy",
    ),
    "memory_files": MemoryFiles(
        workspace_path="/home/user/project",
        files=[
            MemoryFileEntry(
                path="/home/user/project/.tst/memory/MEMORY.md",
                name="MEMORY.md",
                content="durable: ruff\n",
            )
        ],
    ),
    "instruction_files": InstructionFiles(
        workspace_path="/home/user/project",
        files=[
            InstructionFileEntry(
                path="/home/user/project/AGENTS.md", name="AGENTS.md", kind="agents"
            ),
            InstructionFileEntry(
                path="/home/user/project/.tst/rules/api.md", name="api.md", kind="rule"
            ),
        ],
        created=None,
    ),
    "context_pins": ContextPins(
        workspace_path="/home/user/project",
        pins=[
            ContextPinEntry(path="src/app.ts", name="app.ts", kind="file", lines=12),
        ],
    ),
    "context_compacted": ContextCompacted(
        session_id="sess-1",
        dropped_messages=12,
        kept_messages=8,
        tokens_before=12000,
        tokens_after=4000,
        seq=18,
    ),
    "tier_switched": TierSwitched(
        session_id="sess-1",
        tier="worker",
        previous="brain",
        seq=19,
    ),
    "session_list": SessionList(
        sessions=[
            {
                "session_id": "sess-1",
                "workspace_path": "/home/user/project",
                "state": "interrupted",
                "created_at": "2026-08-13T10:00:00Z",
                "updated_at": "2026-08-13T10:00:00Z",
                "event_count": 0,
                "title": "hello world",
            },
            # TD-1715: the list stays complete and marks what is filed away.
            {
                "session_id": "sess-2",
                "workspace_path": "/home/user/project",
                "state": "complete",
                "created_at": "2026-08-13T09:00:00Z",
                "updated_at": "2026-08-13T09:30:00Z",
                "event_count": 12,
                "archived": True,
                "starred": True,
            },
        ]
    ),
    "policy_rules": PolicyRules(
        rules=[
            PolicyRuleSummary(tool="shell", args="npm test", effect="auto"),
            PolicyRuleSummary(tool="fs_*", args="src/**", effect="ask"),
        ]
    ),
    # Onboarding (TD-1101): connection-scoped, seq=1 like session_list/policy_rules.
    "setup_state": SetupState(
        has_api_key=False,
        presets=["budget", "local", "tst-default"],
        active_preset="tst-default",
        skip_all_approvals=False,
        coworker_enabled=True,
        cu_glow=True,
        cu_agent_cursor=True,
        cu_show_on_real_display=False,
        remote_attach_enabled=False,
        remote_bind=None,
        credentials=[
            CredentialSummary(
                id="openrouter",
                name="OpenRouter",
                stored=False,
                base_url="http://127.0.0.1:9/v1",
            ),
        ],
        tier_credentials={"brain": "openrouter", "worker": None, "validator": None},
        tier_loopback={"brain": False, "worker": True, "validator": True},
    ),
    "api_key_validated": ApiKeyValidated(ok=True, detail="Key accepted by provider."),
    # Diagnostics (TD-1104): connection-scoped like setup_state. Mixed rows so
    # consumers see every status — the fail row carries the concrete fix.
    "diagnostics_report": DiagnosticsReport(
        checks=[
            DiagnosticCheck(name="daemon", status="ok", detail="responding (v0.1.0, up 3.2s)"),
            DiagnosticCheck(
                name="api_key",
                status="fail",
                detail="no API key stored in the keychain",
                fix="Open the wizard (gear in the title bar) and store one.",
            ),
            DiagnosticCheck(name="provider", status="skip", detail="not checked — no API key"),
            DiagnosticCheck(name="git", status="ok", detail="git version 2.50.0 (/usr/bin/git)"),
            DiagnosticCheck(name="workspace", status="ok", detail="project is writable"),
            DiagnosticCheck(name="steering", status="ok", detail="3 steering source(s) parsed"),
        ]
    ),
    # Usage and cost (TD-1706): one row per bucket kind, each split by tier,
    # so a consumer sees all three key shapes — session id, day, week start.
    "usage_report": UsageReport(
        rows=[
            UsageRollup(
                bucket="session",
                key="sess-1",
                tier="brain",
                prompt_tokens=12000,
                cached_prompt_tokens=8000,
                completion_tokens=3000,
                cost=0.117,
                classifier_cost=0.0025,
            ),
            UsageRollup(
                bucket="day",
                key="2026-08-17",
                tier="worker",
                prompt_tokens=4000,
                cached_prompt_tokens=0,
                completion_tokens=900,
                cost=0.0136,
            ),
            UsageRollup(
                bucket="week",
                key="2026-08-17",
                tier="validator",
                prompt_tokens=2500,
                cached_prompt_tokens=500,
                completion_tokens=200,
                cost=0.0031,
            ),
        ]
    ),
    "memory_proposal": MemoryProposal(
        session_id="sess-1",
        proposal_id="mp-1",
        files=[
            MemoryFileDiff(
                action="replace",
                path=".tst/memory/MEMORY.md",
                diff=(
                    "--- a/.tst/memory/MEMORY.md\n"
                    "+++ b/.tst/memory/MEMORY.md\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+durable: ruff\n"
                ),
                before="old\n",
                after="durable: ruff\n",
            )
        ],
        seq=21,
    ),
    "usage_exported": UsageExported(
        format="csv",
        path="/home/user/.local/share/tst-desk/exports/usage-20260817T120000Z.csv",
        rows=42,
    ),
    # TD-2901: connection-scoped notice when attach asked for a rotated seq.
    "log_trimmed": LogTrimmed(
        session_id="sess-1",
        requested_from_seq=1,
        earliest_seq=8,
    ),
    # TD-3201: session-scoped notice; list/open replies stay off the log.
    "artifact_ready": ArtifactReady(
        session_id="sess-1",
        artifact_id="art-1",
        title="Notes",
        mime="text/markdown",
        path="notes.md",
        seq=22,
    ),
    "artifact_list": ArtifactList(
        session_id="sess-1",
        artifacts=[
            ArtifactEntry(
                artifact_id="art-1",
                title="Notes",
                mime="text/markdown",
                path="notes.md",
            )
        ],
    ),
    "artifact": Artifact(
        session_id="sess-1",
        artifact_id="art-1",
        title="Notes",
        mime="text/markdown",
        path="notes.md",
    ),
    "screen_frame": ScreenFrame(
        session_id="sess-1",
        path="screens/aa.png",
        mime="image/png",
        width=1,
        height=1,
        tool_call_id="c1",
        seq=23,
    ),
    # TD-3404: process-wide kill-switch. Connection-scoped; no session_id.
    "cu_kill_state": CuKillState(killed=True),
    "design_hit": DesignHit(
        session_id="sess-1",
        x=12.0,
        y=34.0,
        xpath="//*[@data-mock-point='12,34']",
        role="button",
        attributes={"id": "mock-target"},
        box=DesignHitBox(x=0.0, y=24.0, width=80.0, height=24.0),
        styles={"display": "inline-block"},
    ),
    "cu_permissions": CuPermissions(
        granted=False,
        screen_recording=False,
        accessibility=True,
        screen_recording_url=(
            "x-apple.systemsettings:com.apple.preferences.privacy-security.ScreenCapture"
        ),
        accessibility_url=(
            "x-apple.systemsettings:com.apple.preferences.privacy-security.accessibility"
        ),
        first_run=True,
    ),
    "list_jobs": ListJobs(),
    "save_job": SaveJob(
        workspace="/home/user/project",
        instruction="summarize the inbox",
        cadence="every 1 hour",
        deliver_to="window",
    ),
    "delete_job": DeleteJob(job_id="job-1"),
    "job_list": JobList(
        jobs=[
            JobEntry(
                id="job-1",
                workspace="/home/user/project",
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                paused=False,
            )
        ],
    ),
}


def main() -> None:
    output_path = (
        Path(__file__).resolve().parent.parent.parent
        / "ui"
        / "src"
        / "lib"
        / "protocol-fixtures.json"
    )
    fixtures = {}
    for name, msg in FIXTURES.items():
        fixtures[name] = json.loads(msg.model_dump_json())
    # hello_ack is built as a plain dict, not a Pydantic model.
    fixtures["hello_ack"] = {"type": "hello_ack", "version": PROTOCOL_VERSION}
    output_path.write_text(json.dumps(fixtures, indent=2))
    print(f"Wrote {len(fixtures)} fixtures to {output_path}")


if __name__ == "__main__":
    main()
