"""Generate JSON fixtures for all protocol message types.

Used by the TypeScript cross-language test to verify type definitions match.
Writes a JSON file with one sample per message type.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Add core to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.peer_policy import FORWARDED_VERBS
from tstd.protocol import (
    CHUNK_MAX,
    PROTOCOL_VERSION,
    AddMachine,
    AddModel,
    AddPin,
    AfterWriteRun,
    AlwaysAllow,
    AnswerQuestion,
    ApiKeyValidated,
    ApprovalRequest,
    Approve,
    ApproveGrokPlan,
    ArchiveSession,
    Artifact,
    ArtifactChunk,
    ArtifactEntry,
    ArtifactList,
    ArtifactReady,
    AssistantDelta,
    AssistantReasoning,
    AssistantRetract,
    Attach,
    Attachment,
    AttachmentExtracted,
    AutonomyStart,
    AutonomySummary,
    BoundaryUpdate,
    Cancel,
    CancelPairing,
    CharterDocument,
    CheckCuPermissions,
    CheckpointNotice,
    CloseWorktree,
    CommandEntry,
    CommandList,
    ConfigTrust,
    ContextCompacted,
    ContextPinEntry,
    ContextPins,
    ContextUsage,
    ConversationReset,
    CostUpdate,
    CreateRule,
    CredentialSummary,
    CuKillState,
    CuPermissions,
    CuSession,
    DecisionLogged,
    DeleteApiKey,
    DeleteCredential,
    DeleteJob,
    DeleteJobTemplate,
    DeleteMcpSecret,
    DeleteMcpServer,
    DeleteSession,
    Deny,
    DenyVerify,
    DesignHit,
    DesignHitBox,
    DesignHitTest,
    Detach,
    DiagnosticCheck,
    DiagnosticsReport,
    Drain,
    DrainResult,
    EmailTestResult,
    EndpointModel,
    EndpointModels,
    EndSession,
    Error,
    ExportUsage,
    ExtractAttachment,
    FilesRestored,
    FilesRestorePreview,
    FocusWindow,
    ForkFrom,
    GetCharter,
    GetHooks,
    GetInstructionStack,
    GetSessionJobSource,
    GetSetupState,
    GetUsage,
    GithubConnect,
    GithubDisconnect,
    GithubStatus,
    GoogleConnect,
    GoogleDisconnect,
    GoogleStatus,
    GrokCommand,
    GrokCommands,
    GrokExtension,
    GrokExtensions,
    GrokMode,
    GrokPlan,
    GrokPlanEntry,
    GrokPreview,
    GrokSessionEntry,
    GrokSessionList,
    Hello,
    HookInfo,
    HookRun,
    HooksState,
    InstructionFileEntry,
    InstructionFiles,
    InstructionStack,
    JobDraftReply,
    JobEntry,
    JobList,
    JobRunEntry,
    JobRuns,
    JobTemplateEntry,
    JobTemplates,
    LineComment,
    LineCommentAnchor,
    ListArtifacts,
    ListCommands,
    ListEndpointModels,
    ListGrokExtensions,
    ListGrokSessions,
    ListInstructions,
    ListJobRuns,
    ListJobs,
    ListJobTemplates,
    ListMachines,
    ListMemory,
    ListPins,
    ListPolicyRules,
    ListSessions,
    LocalServer,
    LocalServers,
    LogTrimmed,
    LoosenedSetting,
    MachineList,
    MachineSummary,
    McpServerSummary,
    McpValueSummary,
    MemoryAccept,
    MemoryEdit,
    MemoryFileDiff,
    MemoryFileEdit,
    MemoryFileEntry,
    MemoryFiles,
    MemoryProposal,
    MemoryReject,
    MicrosoftConnect,
    MicrosoftDisconnect,
    MicrosoftStatus,
    ModelFallback,
    ModelTested,
    MoveSession,
    OpenArtifact,
    OpenInTerminal,
    OpenWorkspace,
    Pair,
    PairingCode,
    PairingCompleted,
    ParseJob,
    Ping,
    PlanPinned,
    PlanReview,
    PlanReviewed,
    PolicyRules,
    PolicyRuleSummary,
    PreviewRestoreFiles,
    Question,
    QuestionAnswer,
    QuestionAnswered,
    QuestionOption,
    QuestionRequest,
    QueuedMessageEntry,
    QueuedMessages,
    ReadArtifactChunk,
    Ready,
    ReconnectMachine,
    RemoteDeviceSummary,
    RemoveMachine,
    RemovePin,
    RemoveQueued,
    RemoveRemoteDevice,
    RenameSession,
    RestoreFileEntry,
    RestoreFiles,
    Resume,
    ReviewPlan,
    RevokePolicyRule,
    RuleActivated,
    RunDiagnostics,
    RunGrokCommand,
    RunJob,
    RunPermissions,
    RunRule,
    RunVerify,
    SaveCharter,
    SaveJob,
    SaveJobTemplate,
    SaveMemory,
    ScanLocalServers,
    ScreenFrame,
    SearchSessions,
    SendQueued,
    SessionGoal,
    SessionJobSource,
    SessionList,
    SessionSearchHit,
    SessionSearchResults,
    SessionState,
    SessionWorktreeInfo,
    SetApiKey,
    SetBranch,
    SetCoworker,
    SetCredential,
    SetCuIndicators,
    SetCuKill,
    SetCuPolicy,
    SetEffort,
    SetEmailNotify,
    SetEngine,
    SetGrokMode,
    SetJudgments,
    SetLoadGlobalMemory,
    SetMcpSecret,
    SetMcpServer,
    SetPlan,
    SetPreset,
    SetRemoteAttach,
    SetSessionPreset,
    SetSessionStar,
    SetSkipAllApprovals,
    SetTier,
    SetTierCredential,
    SetupState,
    SetVoice,
    SetWorkspacePin,
    ShellJobInfo,
    ShellJobs,
    ShellOutput,
    Shutdown,
    SkillStackEntry,
    StartAutonomy,
    StartPairing,
    SteeringAccept,
    SteeringDecision,
    SteeringEdit,
    SteeringFileDiff,
    SteeringProposal,
    SteeringReject,
    SteeringReloaded,
    SteerPoint,
    StopCheck,
    StopCheckItem,
    SubmitLineComments,
    TaskList,
    TaskListItem,
    TestEmail,
    TestModel,
    TierState,
    TierSwitched,
    ToolCall,
    ToolProgress,
    ToolResult,
    Transcribe,
    Transcript,
    TrustWorkspaceConfig,
    TurnComplete,
    UsageExported,
    UsageReport,
    UsageRollup,
    UserMessage,
    UserTurn,
    ValidateApiKey,
    VerifyResult,
    VerifyStarted,
    WorkerStep,
    WorktreeState,
    build_hello_ack,
    build_pair_ok,
)

# TD-5143: one sent file shared by the samples, so a UI test can pull the
# whole file from ``artifact_chunk`` and verify it against ``artifact_ready``.
# Its bytes are "hi" (``data_b64`` "aGk=").
_SENT_ID = "0" * 32
_SENT_SHA256 = hashlib.sha256(b"hi").hexdigest()

_STEERING_DIFF = SteeringFileDiff(
    action="replace",
    path="AGENTS.md",
    diff=(
        "--- a/AGENTS.md\n"
        "+++ b/AGENTS.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Project\n"
        " \n"
        " - Use uv.\n"
        "+- Run ruff first.\n"
    ),
    before="# Project\n\n- Use uv.\n",
    after="# Project\n\n- Use uv.\n- Run ruff first.\n",
)

FIXTURES = {
    # Client messages
    "hello": Hello(token="test-token-abc", version=1, features=["machines"]),
    "open_workspace": OpenWorkspace(path="/home/user/project"),
    "user_message": UserMessage(session_id="sess-1", content="hello world"),
    # TD-5155: the person's choice on a message a restart left held.
    "send_queued": SendQueued(session_id="sess-1", queued_id="q-1"),
    "remove_queued": RemoveQueued(session_id="sess-1", queued_id="q-1"),
    # TD-5132: the diff view's line comments, sent as one message.
    "submit_line_comments": SubmitLineComments(
        session_id="sess-1",
        comments=[
            LineComment(
                id="c-1",
                path="/home/user/project/src/app.py",
                start_line=4,
                end_line=5,
                body="Use a constant here",
                lines=["x = 3", "y = 3"],
                base_seq=7,
            )
        ],
    ),
    "fork_from": ForkFrom(session_id="sess-1", user_index=0, content="hello again"),
    "set_branch": SetBranch(session_id="sess-1", user_index=0, sibling_index=1),
    # TD-5125: "Restore files to here" and the preview it shows first.
    # TD-5131: the composer's chip count for a staged document.
    "extract_attachment": ExtractAttachment(
        session_id="sess-1",
        request_id="att-1",
        attachment=Attachment(name="report.pdf", content_b64="JVBERi0xLjQK"),
    ),
    "preview_restore_files": PreviewRestoreFiles(session_id="sess-1", turn_id="turn-1"),
    "restore_files": RestoreFiles(
        session_id="sess-1",
        turn_id="turn-1",
        plan_id="0123456789abcdef0123",
        include_hand_edited=["notes.md"],
    ),
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
    # TD-5112: Send now mid-turn asks to join the running turn.
    "user_message_steer": UserMessage(
        session_id="sess-1", content="Also run the linter", steer=True
    ),
    "approve": Approve(session_id="sess-1", tool_call_id="tc-1"),
    "deny": Deny(session_id="sess-1", tool_call_id="tc-1", reason="not safe"),
    "deny_no_reason": Deny(session_id="sess-1", tool_call_id="tc-1"),
    "always_allow": AlwaysAllow(session_id="sess-1", tool_call_id="tc-1"),
    "answer_question": AnswerQuestion(
        session_id="sess-1",
        tool_call_id="tc-q",
        answers=[
            QuestionAnswer(selected=["Postgres"]),
            QuestionAnswer(selected=["Auth", "Billing"], text="and audit logs"),
        ],
    ),
    "answer_question_declined": AnswerQuestion(
        session_id="sess-1", tool_call_id="tc-q", declined=True
    ),
    # TD-5123: approve the plan as edited, or reject it with feedback.
    "review_plan": ReviewPlan(
        session_id="sess-1",
        tool_call_id="tc-p",
        decision="approve",
        plan="1. Add greet().",
        feedback="keep it small",
    ),
    "review_plan_reject": ReviewPlan(
        session_id="sess-1", tool_call_id="tc-p", decision="reject", feedback="use the logger"
    ),
    "list_policy_rules": ListPolicyRules(session_id="sess-1"),
    "revoke_policy_rule": RevokePolicyRule(session_id="sess-1", tool="shell", args="rm *"),
    # TD-5113: the hash the trust card showed. A placeholder, not a real file's.
    "trust_workspace_config": TrustWorkspaceConfig(
        session_id="sess-1", sha256="0" * 64, trust=True
    ),
    "set_skip_all_approvals": SetSkipAllApprovals(enabled=True),
    "set_load_global_memory": SetLoadGlobalMemory(enabled=True),
    "set_coworker": SetCoworker(enabled=True),
    "set_voice": SetVoice(enabled=True),
    "transcribe": Transcribe(audio_b64="AAAA", mime="audio/webm"),
    "set_email_notify": SetEmailNotify(
        enabled=True,
        method="smtp",
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="desk",
        from_address="desk@example.com",
        password=None,
    ),
    "test_email": TestEmail(to="owner@example.com"),
    "github_connect": GithubConnect(),
    "github_disconnect": GithubDisconnect(),
    "microsoft_connect": MicrosoftConnect(),
    "microsoft_disconnect": MicrosoftDisconnect(),
    "google_connect": GoogleConnect(),
    "google_disconnect": GoogleDisconnect(),
    "set_cu_indicators": SetCuIndicators(glow=True, agent_cursor=True, show_on_real_display=False),
    "set_cu_policy": SetCuPolicy(
        enabled=True,
        mode="background",
        unhide_on_finish=True,
        denied_apps=["1Password"],
    ),
    "set_judgments": SetJudgments(
        verification=True,
        semantic_breaker=True,
        candidate_selection=False,
        confidence_threshold=0.6,
        max_state_chars=2000,
        backend="typesafe",
    ),
    "set_workspace_pin": SetWorkspacePin(path="/home/user/project", pinned=True),
    "resume": Resume(session_id="sess-1"),
    "cancel": Cancel(session_id="sess-1"),
    "run_verify": RunVerify(session_id="sess-1"),
    "deny_verify": DenyVerify(session_id="sess-1"),
    "attach": Attach(session_id="sess-1", from_seq=5),
    "detach": Detach(session_id="sess-1"),
    "set_tier": SetTier(session_id="sess-1", tier="brain"),
    "set_effort": SetEffort(session_id="sess-1", effort="ultracode"),
    "set_plan": SetPlan(session_id="sess-1", on=True),
    "set_session_preset": SetSessionPreset(session_id="sess-1", name="budget"),
    "get_instruction_stack": GetInstructionStack(session_id="sess-1"),
    "get_hooks": GetHooks(session_id="sess-1"),
    "list_instructions": ListInstructions(workspace_path="/home/user/project"),
    "list_commands": ListCommands(workspace_path="/home/user/project"),
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
    "search_sessions": SearchSessions(query="date parser", limit=50),
    "add_pin": AddPin(workspace_path="/home/user/project", path="src/app.ts"),
    "remove_pin": RemovePin(workspace_path="/home/user/project", path="src/app.ts"),
    "memory_accept": MemoryAccept(session_id="sess-1", proposal_id="mp-1"),
    "memory_edit": MemoryEdit(
        session_id="sess-1",
        proposal_id="mp-1",
        files=[MemoryFileEdit(path=".tst/memory/MEMORY.md", content="durable: ruff\n")],
    ),
    "memory_reject": MemoryReject(session_id="sess-1", proposal_id="mp-1"),
    # TD-5140: the user's decision on an agent-proposed steering edit.
    "steering_accept": SteeringAccept(session_id="sess-1", proposal_id="sp-1"),
    "steering_edit": SteeringEdit(
        session_id="sess-1", proposal_id="sp-1", content="# Project\n\n- Run ruff first.\n"
    ),
    "steering_reject": SteeringReject(session_id="sess-1", proposal_id="sp-1"),
    "end_session": EndSession(session_id="sess-1"),
    "shutdown": Shutdown(),
    # Before a deploy restart (TD-5023); 0/0 releases the hold.
    "drain": Drain(timeout_s=120, hold_s=120),
    "drain_release": Drain(timeout_s=0, hold_s=0),
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
    # TD-5126: no action asks the daemon; an unchanged worktree just goes.
    "close_worktree": CloseWorktree(session_id="sess-1"),
    "close_worktree_merge": CloseWorktree(session_id="sess-1", action="merge"),
    # Onboarding (TD-1101 first-run wizard)
    "get_setup_state": GetSetupState(),
    "set_api_key": SetApiKey(api_key="sk-or-test-key"),
    "delete_api_key": DeleteApiKey(),
    "validate_api_key": ValidateApiKey(),
    # "Add a model" walkthrough (TD-5172)
    "list_endpoint_models": ListEndpointModels(
        base_url="https://api.example.com/v1", api_key="sk-test-key"
    ),
    # "Find models on this computer" (TD-5180)
    "scan_local_servers": ScanLocalServers(),
    "add_model": AddModel(
        label="My model",
        description="General coding and chat",
        base_url="https://api.example.com/v1",
        slug="vendor/model-a",
        credential="openrouter",
        context_window=128000,
    ),
    "test_model": TestModel(),
    "set_preset": SetPreset(name="tst-default"),
    "set_engine": SetEngine(kind="grok"),
    "set_grok_mode": SetGrokMode(session_id="sess-1", mode="plan"),
    "run_grok_command": RunGrokCommand(session_id="sess-1", name="compact", argument="keep tests"),
    "list_grok_sessions": ListGrokSessions(),
    "open_in_terminal": OpenInTerminal(session_id="sess-1"),
    "approve_grok_plan": ApproveGrokPlan(session_id="sess-1", comment="go"),
    "list_grok_extensions": ListGrokExtensions(),
    "set_credential": SetCredential(name="Local"),
    "delete_credential": DeleteCredential(credential="local"),
    "set_tier_credential": SetTierCredential(preset="local", tier="brain", credential="local"),
    "set_mcp_server": SetMcpServer(
        id="example",
        transport="stdio",
        command=["python", "-m", "some_mcp"],
        enabled=True,
    ),
    "delete_mcp_server": DeleteMcpServer(id="example"),
    # TD-5110: the value is a placeholder, never a real token.
    "set_mcp_secret": SetMcpSecret(
        server_id="example", target="env", name="GITHUB_TOKEN", value="placeholder-value"
    ),
    "delete_mcp_secret": DeleteMcpSecret(server_id="example", target="env", name="GITHUB_TOKEN"),
    # Diagnostics (TD-1104 doctor)
    "run_diagnostics": RunDiagnostics(),
    # Usage and cost (TD-1706)
    "get_usage": GetUsage(),
    "export_usage": ExportUsage(format="csv"),
    # Artifacts (TD-3201): list/open on the wire; record is a daemon API.
    "list_artifacts": ListArtifacts(session_id="sess-1"),
    "open_artifact": OpenArtifact(session_id="sess-1", artifact_id="art-1"),
    # TD-5143: bytes of a sent file, by id; never a path.
    "read_artifact_chunk": ReadArtifactChunk(
        session_id="sess-1", artifact_id=_SENT_ID, offset=0, length=CHUNK_MAX
    ),
    "design_hit_test": DesignHitTest(session_id="sess-1", x=12.0, y=34.0),
    "check_cu_permissions": CheckCuPermissions(),
    "set_cu_kill": SetCuKill(killed=True),
    "set_remote_attach": SetRemoteAttach(enabled=True),
    # Phone pairing (TD-5014). `pair` is a first frame like `hello`.
    "pair": Pair(code="ABCD2345", device_name="Pixel", version=1),
    # A hub pairing with another computer (TD-5015).
    "pair_machine": Pair(code="ABCD2345", device_name="desk", version=1, kind="machine"),
    "start_pairing": StartPairing(),
    "cancel_pairing": CancelPairing(),
    "remove_remote_device": RemoveRemoteDevice(device_id="dev_0123456789ab"),
    # Other computers (TD-5015). Loopback only; the code is a placeholder.
    "add_machine": AddMachine(link="tstdesk://pair?h=100.64.1.5&p=39079&c=ABCD2345&n=tower"),
    "add_machine_address": AddMachine(host="100.64.1.5", port=39079, code="ABCD-2345"),
    "remove_machine": RemoveMachine(machine="mch_0123456789ab"),
    "list_machines": ListMachines(),
    "reconnect_machine": ReconnectMachine(machine="mch_0123456789ab"),
    # Daemon events
    "ready": Ready(version="0.1.0", protocol_version=PROTOCOL_VERSION),
    "session_state": SessionState(session_id="sess-1", state="running", engine="native", seq=2),
    "conversation_reset": ConversationReset(
        session_id="sess-1",
        user_index=0,
        sibling_index=1,
        sibling_count=2,
        content="hello again",
        seq=2,
    ),
    "attachment_extracted": AttachmentExtracted(
        session_id="sess-1",
        request_id="att-1",
        name="report.pdf",
        kind="pdf",
        units=3,
        text_bytes=5120,
        tokens=1280,
    ),
    "attachment_extracted_refused": AttachmentExtracted(
        session_id="sess-1",
        request_id="att-2",
        name="scan.pdf",
        error_code="attachment_no_text",
        message="scan.pdf has no text to read.",
    ),
    "files_restore_preview": FilesRestorePreview(
        session_id="sess-1",
        turn_id="turn-1",
        plan_id="0123456789abcdef0123",
        files=[
            RestoreFileEntry(path="src/app.py", change="modify"),
            RestoreFileEntry(path="src/new.py", change="delete"),
            RestoreFileEntry(path="notes.md", change="modify", hand_edited=True),
            RestoreFileEntry(path="old.txt", change="recreate"),
        ],
        diff=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
        diff_truncated=False,
    ),
    "files_restore_preview_empty": FilesRestorePreview(
        session_id="sess-1", turn_id="turn-2", plan_id="none"
    ),
    "files_restored": FilesRestored(
        session_id="sess-1",
        turn_id="turn-1",
        restored=["src/app.py", "old.txt"],
        removed=["src/new.py"],
        left_alone=["notes.md"],
        lfs_pointers=[],
        seq=9,
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
    # TD-5112: a message sent mid-turn, handed to that turn after a batch.
    "user_turn_steer": UserTurn(
        session_id="sess-1",
        turn_id="turn-2",
        content="Also run the linter",
        steer=SteerPoint(turn_id="turn-1", step=2, after_tool_call_ids=["tc-1", "tc-2"]),
        seq=9,
    ),
    # TD-5132: a turn that carried the diff view's line comments.
    "user_turn_line_comments": UserTurn(
        session_id="sess-1",
        turn_id="turn-4",
        content="Review comments on your changes (1 comment).",
        line_comments=[
            LineCommentAnchor(
                id="c-1",
                path="/home/user/project/src/app.py",
                start_line=4,
                end_line=5,
                body="Use a constant here",
                base_seq=7,
            )
        ],
        seq=12,
    ),
    # TD-5118: a tst run turn echoes the permissions it runs under.
    "user_turn_run_permissions": UserTurn(
        session_id="sess-1",
        turn_id="turn-3",
        content="Run the tests",
        run_permissions=RunPermissions(
            allow=[RunRule(tool="shell", args="npm test*")],
            deny=[RunRule(tool="fs_write")],
            on_ask="deny_and_continue",
        ),
        seq=12,
    ),
    # TD-5155: messages a restart left held, whole each time.
    "queued_messages": QueuedMessages(
        session_id="sess-1",
        messages=[
            QueuedMessageEntry(queued_id="q-1", content="Also check b.txt"),
            QueuedMessageEntry(
                queued_id="q-2",
                content="Look at this\n\n[Images attached: shot.png (2048 bytes)]",
                images_dropped=1,
            ),
        ],
        seq=4,
    ),
    "assistant_delta": AssistantDelta(session_id="sess-1", delta="Hello ", seq=3),
    "assistant_reasoning": AssistantReasoning(session_id="sess-1", delta="Let me think", seq=3),
    # TD-5159: a model response's text carries its id, and a stall withdraws it.
    "assistant_delta_response": AssistantDelta(
        session_id="sess-1", delta="The fix is ", response_id="resp-1", seq=3
    ),
    "assistant_retract": AssistantRetract(
        session_id="sess-1", response_id="resp-1", reason="stream_stalled", seq=4
    ),
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
    "tool_result_read_only": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="success",
        output="exit 0",
        seq=8,
        allowed_by=["git log", "head"],
    ),
    "tool_result_delegate": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-9",
        status="success",
        output="animals.md names one animal pair.",
        seq=9,
        transcript=[
            WorkerStep(kind="task", text="Which pair is in animals.md?"),
            WorkerStep(kind="tool_call", text='{"path": "animals.md"}', tool="fs_read"),
            WorkerStep(
                kind="tool_result",
                text="pair: zebra-quokka-7731",
                tool="fs_read",
                status="success",
            ),
            WorkerStep(kind="reply", text="animals.md names one animal pair."),
        ],
    ),
    "tool_result_after_write": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="success",
        output="wrote 9 bytes; created a.py\n\n[after-write hook `ruff check` failed: exit 1]",
        seq=10,
        hooks=[
            AfterWriteRun(
                name="ruff format", status="passed", exit_code=0, duration_ms=120, changed=True
            ),
            AfterWriteRun(name="ruff check", status="failed", exit_code=1, duration_ms=310),
        ],
    ),
    "tool_result_denied": ToolResult(
        session_id="sess-1",
        tool_call_id="tc-1",
        status="error",
        output="Denied by user: not safe",
        seq=20,
        error_code="approval_denied",
    ),
    "question_request": QuestionRequest(
        session_id="sess-1",
        tool_call_id="tc-q",
        questions=[
            Question(
                question="Which database?",
                header="Database",
                options=[
                    QuestionOption(label="Postgres", description="Already in docker-compose"),
                    QuestionOption(label="SQLite"),
                ],
            ),
            Question(
                question="Which features first?",
                options=[
                    QuestionOption(label="Auth"),
                    QuestionOption(label="Billing"),
                    QuestionOption(label="Search"),
                ],
                multi_select=True,
            ),
        ],
        seq=7,
    ),
    "question_answered": QuestionAnswered(
        session_id="sess-1",
        tool_call_id="tc-q",
        outcome="answered",
        answers=[
            QuestionAnswer(selected=["Postgres"]),
            QuestionAnswer(selected=["Auth", "Billing"], text="and audit logs"),
        ],
        seq=8,
    ),
    "question_answered_declined": QuestionAnswered(
        session_id="sess-1", tool_call_id="tc-q", outcome="declined", seq=8
    ),
    # TD-5123: read-only plan mode ends with a plan the person reviews.
    "plan_review": PlanReview(
        session_id="sess-1",
        tool_call_id="tc-p",
        plan="## Plan\n1. Add greet() to src/app.py.\n2. Test it.",
        seq=9,
    ),
    "plan_reviewed": PlanReviewed(
        session_id="sess-1",
        tool_call_id="tc-p",
        outcome="approved",
        plan="1. Add greet().",
        edited=True,
        feedback="keep it small",
        seq=11,
    ),
    "plan_reviewed_rejected": PlanReviewed(
        session_id="sess-1",
        tool_call_id="tc-p",
        outcome="rejected",
        feedback="use the logger",
        seq=11,
    ),
    "plan_pinned": PlanPinned(session_id="sess-1", plan="1. Add greet().", seq=10),
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
        proposed_always_allow=PolicyRuleSummary(
            tool="shell", args="npm test", effect="auto", exact="npm test"
        ),
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
    "verify_started": VerifyStarted(session_id="sess-1", seq=10),
    "verify_result": VerifyResult(
        session_id="sess-1",
        verdict="pass",
        summary="Diff matches the tests.",
        cost=0.002,
        pending=False,
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
    # TD-5113: a repo config that loosens settings, not yet trusted.
    "boundary_update_config_trust": BoundaryUpdate(
        session_id="sess-1",
        writable_paths=["**"],
        allowed_commands=[],
        network="deny",
        spend_usd=25.0,
        wall_clock_hours=8.0,
        max_iterations=200,
        source="defaults — workspace config not trusted yet",
        config_trust=ConfigTrust(
            state="pending",
            sha256="0" * 64,
            loosened=[
                LoosenedSetting(
                    key="policy.rules", value="shell: npm * → auto", default="ask (class B and C)"
                ),
                LoosenedSetting(key="boundary.network", value="registry.npmjs.org", default="deny"),
            ],
        ),
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
        preset="test",
        hosts={
            "brain": "mock.local",
            "worker": "mock.local",
            "validator": "mock.local",
        },
        vision=False,
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
        preset="test",
        hosts={
            "brain": "mock.local",
            "worker": "mock.local",
            "validator": "mock.local",
        },
        seq=19,
    ),
    "tier_state_plan": TierState(
        session_id="sess-1",
        tier="brain",
        override=None,
        model_slugs={
            "brain": "test-brain-slug",
            "worker": "test-worker-slug",
            "validator": "test-validator-slug",
        },
        preset="test",
        hosts={
            "brain": "mock.local",
            "worker": "mock.local",
            "validator": "mock.local",
        },
        plan=True,
        seq=20,
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
    "tool_progress": ToolProgress(
        session_id="sess-1",
        tool_call_id="tc-1",
        progress=3,
        total=10,
        message="rendering frames",
        seq=16,
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
        skills=[
            SkillStackEntry(
                name="review",
                description="Review a PR",
                source="workspace",
                loaded=False,
                tokens=40,
            ),
            # TD-5107: loaded although its frontmatter asked for things
            # TST Desk does not do; user-only from disable-model-invocation.
            SkillStackEntry(
                name="deploy",
                description="Deploy to staging",
                source="claude_workspace",
                loaded=False,
                tokens=120,
                ignored_keys=["context", "model"],
                user_only=True,
            ),
        ],
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
    "autonomy_summary": AutonomySummary(
        session_id="sess-1",
        reason="definition of done met",
        branch="tst/auto/ship-the-csv-importer",
        ledger_path=".tst/autonomy/DECISIONS.md",
        changed=["src/importer.py"],
        refusals=[],
        steering_proposals=["AGENTS.md (replace)"],
        ledger_excerpt=(
            "## 2026-08-26T12:00:00Z · Class A · commit abc123\n**Chose:** format\n**Why:** ruff"
        ),
        seq=12,
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
    "command_list": CommandList(
        workspace_path="/home/user/project",
        commands=[
            CommandEntry(
                name="review",
                description="Review the current diff",
                source="workspace",
                body="Please review the staged changes.\n",
            )
        ],
    ),
    "session_search_results": SessionSearchResults(
        seq=1,
        query="date parser",
        hits=[
            SessionSearchHit(
                session_id="11111111-2222-3333-4444-555555555555",
                turn=3,
                role="assistant",
                snippet="…the date parser now reads ISO weeks…",
            )
        ],
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
    "context_compacted_manual": ContextCompacted(
        session_id="sess-1",
        dropped_messages=6,
        kept_messages=4,
        tokens_before=30000,
        tokens_after=9000,
        trigger="manual",
        guidance="keep the parser work",
        seq=18,
    ),
    # The in-turn tool-result trim (TD-5150): dropped_messages counts the
    # results trimmed, and every message stays.
    "context_compacted_inflight_trim": ContextCompacted(
        session_id="sess-1",
        dropped_messages=3,
        kept_messages=14,
        tokens_before=30500,
        tokens_after=29300,
        trigger="inflight_trim",
        seq=18,
    ),
    # Clearing earlier turns' tool results (TD-5115): dropped_messages
    # counts the results cleared, and every message stays.
    "context_compacted_clear_stale": ContextCompacted(
        session_id="sess-1",
        dropped_messages=5,
        kept_messages=34,
        tokens_before=23123,
        tokens_after=13153,
        trigger="clear_stale",
        seq=18,
    ),
    "context_usage": ContextUsage(
        session_id="sess-1",
        tier="brain",
        used_tokens=12345,
        context_window=65536,
        compact_at=49152,
        seq=18,
    ),
    "task_list": TaskList(
        session_id="sess-1",
        items=[
            TaskListItem(content="Read the failing test", status="done"),
            TaskListItem(content="Fix the date parser", status="in_progress"),
            TaskListItem(content="Run the suite", status="pending"),
        ],
        seq=19,
    ),
    "shell_jobs": ShellJobs(
        session_id="sess-1",
        jobs=[
            ShellJobInfo(
                job_id="job-1", command="npm run dev", status="running", started_at=1760000000.0
            ),
            ShellJobInfo(
                job_id="job-2",
                command="npm test -- --watch",
                status="exited",
                exit_code=1,
                started_at=1760000010.0,
                ended_at=1760000070.0,
            ),
        ],
        seq=19,
    ),
    "stop_check": StopCheck(
        session_id="sess-1",
        outcome="retry",
        attempt=1,
        max_retries=3,
        items=[
            StopCheckItem(
                item="$ uv run pytest -q",
                source="workspace",
                via="shell",
                passed=False,
                detail="exit code: 1\n── stdout ──\nFAILED tests/test_dates.py::test_leap_day",
            ),
            StopCheckItem(
                item="The parser handles leap days",
                source="goal",
                via="worker",
                passed=True,
                detail="GREEN",
            ),
        ],
        seq=20,
    ),
    "hook_run": HookRun(
        session_id="sess-1",
        hook_id="user:before_tool:0",
        source="user",
        event="before_tool",
        command="~/bin/no-force-push.sh",
        result="block",
        exit_code=2,
        reason="no force pushes",
        output="no force pushes",
        duration_ms=41,
        tool_name="shell",
        tool_call_id="call-1",
        seq=21,
    ),
    "hooks_state": HooksState(
        session_id="sess-1",
        hooks=[
            HookInfo(
                id="user:before_tool:0",
                source="user",
                event="before_tool",
                command="~/bin/no-force-push.sh",
                name="no force push",
                timeout_seconds=10,
                tools=["shell"],
                last=HookRun(
                    session_id="sess-1",
                    hook_id="user:before_tool:0",
                    source="user",
                    event="before_tool",
                    command="~/bin/no-force-push.sh",
                    result="block",
                    exit_code=2,
                    reason="no force pushes",
                    output="no force pushes",
                    duration_ms=41,
                    tool_name="shell",
                    tool_call_id="call-1",
                    seq=21,
                ),
            ),
        ],
        workspace_state="pending",
        seq=1,
    ),
    "session_goal": SessionGoal(
        session_id="sess-1",
        goal="The parser handles leap days",
        change="set",
        seq=21,
    ),
    "tier_switched": TierSwitched(
        session_id="sess-1",
        tier="worker",
        previous="brain",
        seq=19,
    ),
    # TD-5120: the brain's endpoint was down; the turn moved to its first
    # backup, and the gauge and title bar name the backup.
    "model_fallback": ModelFallback(
        session_id="sess-1",
        tier="brain",
        from_model="test-brain-slug",
        from_host="127.0.0.1:8080",
        to_model="test-backup-slug",
        to_host="backup.local",
        fallback=1,
        reason="connection_error",
        seq=20,
    ),
    "context_usage_fallback": ContextUsage(
        session_id="sess-1",
        tier="brain",
        used_tokens=12345,
        context_window=32768,
        compact_at=19660,
        model="test-backup-slug",
        fallback=1,
        seq=21,
    ),
    "tier_state_fallback": TierState(
        session_id="sess-1",
        tier="brain",
        model_slugs={
            "brain": "test-backup-slug",
            "worker": "test-worker-slug",
            "validator": "test-validator-slug",
        },
        preset="test",
        hosts={
            "brain": "backup.local",
            "worker": "mock.local",
            "validator": "mock.local",
        },
        fallbacks={"brain": 1},
        seq=22,
    ),
    # TD-5126: a session working in its own git worktree.
    "session_list_worktree": SessionList(
        sessions=[
            {
                "session_id": "sess-1",
                "workspace_path": "/home/user/project",
                "state": "running",
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
                "event_count": 3,
                "worktree": SessionWorktreeInfo(
                    branch="tst/wt/sess-1ab",
                    path="/home/user/.local/share/tst-desk/worktrees/project-sess-1ab",
                    main_dirty=2,
                    setup="running",
                ),
            },
        ]
    ),
    "worktree_state": WorktreeState(
        session_id="sess-1",
        worktree=SessionWorktreeInfo(
            branch="tst/wt/sess-1ab",
            path="/home/user/.local/share/tst-desk/worktrees/project-sess-1ab",
            setup="failed",
            setup_note="Setup exited with 1: npm ci",
        ),
        note="Setup exited with 1: npm ci",
        seq=20,
    ),
    "worktree_state_closed": WorktreeState(
        session_id="sess-1",
        note="Merged tst/wt/sess-1ab into the main checkout.",
        seq=21,
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
                "preset": "tst-default",
                "busy": False,
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
                "busy": False,
            },
        ]
    ),
    "policy_rules": PolicyRules(
        rules=[
            PolicyRuleSummary(tool="shell", args="npm test", effect="auto", exact="npm test"),
            PolicyRuleSummary(tool="fs_*", args="src/**", effect="ask"),
            # TD-5149: Always allow escapes glob characters; `exact` reads them back.
            PolicyRuleSummary(
                tool="fs_write",
                args="app/[[]id]/page.tsx",
                effect="auto",
                exact="app/[id]/page.tsx",
            ),
        ]
    ),
    # Onboarding (TD-1101): connection-scoped, seq=1 like session_list/policy_rules.
    "setup_state": SetupState(
        has_api_key=False,
        presets=["budget", "local", "tst-default"],
        active_preset="tst-default",
        preset_labels={"tst-default": "Default", "budget": "Budget"},
        preset_descriptions={"tst-default": "General coding and chat"},
        team_presets=["budget"],
        has_model=True,
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
                base_url=None,
            ),
        ],
        tier_credentials={"brain": "openrouter", "worker": None, "validator": None},
        tier_loopback={"brain": False, "worker": True, "validator": True},
        mcp_servers=[
            McpServerSummary(
                id="example",
                transport="stdio",
                command=["python", "-m", "some_mcp"],
                url="",
                enabled=True,
                env=[
                    McpValueSummary(
                        name="GITHUB_TOKEN", source="keychain", entry="github_token", stored=True
                    ),
                    McpValueSummary(name="LOG_LEVEL", source="literal"),
                ],
            )
        ],
        speech_enabled=False,
        speech_ready=False,
    ),
    # Phone pairing (TD-5014): connection-scoped, seq=1. Made-up code and host.
    "pairing_code": PairingCode(
        code="ABCD2345",
        display_code="ABCD-2345",
        host="100.64.1.5",
        port=48123,
        desktop_name="desk",
        link="tstdesk://pair?h=100.64.1.5&p=48123&c=ABCD2345&n=desk",
        qr_svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29"></svg>',
        expires_at="2026-10-14T10:50:00Z",
    ),
    "pairing_completed": PairingCompleted(
        device=RemoteDeviceSummary(
            id="dev_0123456789ab", name="Pixel", paired_at="2026-10-14T10:41:00Z"
        )
    ),
    # One connected computer and one self-pair that is unreachable (TD-5015).
    "machine_list": MachineList(
        machines=[
            MachineSummary(
                id="mch_0123456789ab",
                name="TOWER",
                host="100.64.1.5",
                port=39079,
                status="connected",
                last_seen="2026-10-14T10:42:00Z",
                paired_at="2026-10-14T10:41:00Z",
            ),
            MachineSummary(
                id="mch_ba9876543210",
                name="desk",
                host="100.64.1.9",
                port=39079,
                status="unreachable",
                paired_at="2026-10-14T10:41:00Z",
                same_computer=True,
                detail="Can't reach desk. Trying again in 30 s.",
            ),
        ]
    ),
    # A drain that timed out with one turn still working and one parked on
    # an approval; the hold runs on (TD-5023).
    "drain_result": DrainResult(
        drained=False,
        running=["s1"],
        paused=["s3"],
        awaiting_approval=["s2"],
        hold_until="2026-10-14T10:47:00+00:00",
    ),
    "api_key_validated": ApiKeyValidated(ok=True, detail="Key accepted by provider."),
    "endpoint_models": EndpointModels(
        base_url="https://api.example.com/v1",
        models=[
            EndpointModel(
                id="vendor/model-a", context_window=128000, input_price=3.0, output_price=15.0
            ),
            EndpointModel(id="vendor/model-b"),
        ],
    ),
    "endpoint_models_error": EndpointModels(
        base_url="https://api.example.com/v1",
        error="The server refused the key. Check it and try again.",
    ),
    "model_tested": ModelTested(preset="tst-default", ok=True, reply="I am working."),
    "local_servers": LocalServers(
        servers=[
            LocalServer(
                name="Ollama",
                base_url="http://127.0.0.1:11434/v1",
                models=[EndpointModel(id="llama3.2:3b"), EndpointModel(id="qwen3:8b")],
            )
        ],
        checked=["http://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1"],
    ),
    "local_servers_none": LocalServers(checked=["http://127.0.0.1:11434/v1"]),
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
            DiagnosticCheck(
                name="skills",
                status="ok",
                detail="2 skill(s) found; frontmatter keys ignored: deploy (context, model)",
            ),
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
    # TD-5140: an agent-proposed steering edit, and the user's decision.
    "steering_proposal": SteeringProposal(
        session_id="sess-1",
        proposal_id="sp-1",
        files=[_STEERING_DIFF],
        tool_call_id="call-1",
        seq=22,
    ),
    "steering_proposal_fresh": SteeringProposal(
        session_id="sess-1",
        proposal_id="sp-2",
        files=[_STEERING_DIFF],
        tool_call_id="call-1",
        replaces="sp-1",
        autonomy=True,
        seq=24,
    ),
    "steering_decision": SteeringDecision(
        session_id="sess-1",
        proposal_id="sp-2",
        decision="accepted",
        files=[_STEERING_DIFF],
        seq=25,
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
    # TD-5143: the notice is a sent file, the kind with every optional field.
    "artifact_ready": ArtifactReady(
        session_id="sess-1",
        artifact_id=_SENT_ID,
        title="app-release.apk",
        mime="application/vnd.android.package-archive",
        path=f"artifacts/{_SENT_ID}",
        kind="sent_file",
        name="app-release.apk",
        size=2,
        sha256=_SENT_SHA256,
        caption="0.1.9 release build",
        tool_call_id="call_abc",
        seq=22,
    ),
    # One TD-3201 artifact and the TD-5143 sent file, so both shapes ride.
    "artifact_list": ArtifactList(
        session_id="sess-1",
        artifacts=[
            ArtifactEntry(
                artifact_id="art-1",
                title="Notes",
                mime="text/markdown",
                path="notes.md",
            ),
            ArtifactEntry(
                artifact_id=_SENT_ID,
                title="app-release.apk",
                mime="application/vnd.android.package-archive",
                path=f"artifacts/{_SENT_ID}",
                kind="sent_file",
                name="app-release.apk",
                size=2,
                sha256=_SENT_SHA256,
                caption="0.1.9 release build",
                tool_call_id="call_abc",
            ),
        ],
    ),
    "artifact": Artifact(
        session_id="sess-1",
        artifact_id="art-1",
        title="Notes",
        mime="text/markdown",
        path="notes.md",
    ),
    # TD-5143: the whole sent file in one chunk; connection-scoped, seq 1.
    "artifact_chunk": ArtifactChunk(
        session_id="sess-1",
        artifact_id=_SENT_ID,
        offset=0,
        size=2,
        sha256=_SENT_SHA256,
        data_b64="aGk=",
        eof=True,
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
    # TD-3407: episode open/close. Session-scoped; it lives in the session log.
    "cu_session": CuSession(session_id="sess-1", active=True, seq=24),
    "focus_window": FocusWindow(reason="cu_session_closed"),
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
        session_type="",
        wayland="",
        wayland_applies=False,
        xtest="",
        xtest_applies=False,
        no_display="",
        no_display_applies=False,
    ),
    "list_jobs": ListJobs(),
    "save_job": SaveJob(
        workspace="/home/user/project",
        instruction="summarize the inbox",
        cadence="every 1 hour",
        deliver_to="window",
        # TD-3812: a real pin, not nulls — an all-null sample would not
        # prove the TypeScript side accepts the values.
        preset="vllm",
        engine="native",
        # TD-3813: a phrase, not null — the job stores the seconds.
        grace="2 hours",
        # TD-3814: a count and a phrase, not nulls.
        retries=2,
        retry_delay="10 minutes",
        # TD-3819: a phrase, not null — the job stores the seconds.
        max_run="20 minutes",
        # TD-3817: a real follow-on id, not null.
        then="job-2",
        # TD-3818: a real local calendar, not nulls.
        skip_calendar="/home/user/holidays.ics",
        skip_match="holiday|PTO|OOO",
    ),
    "delete_job": DeleteJob(job_id="job-1"),
    "parse_job": ParseJob(text="every 2 hours in /ws/proj summarize the inbox deliver to slack"),
    "run_job": RunJob(job_id="job-1"),
    "list_job_runs": ListJobRuns(job_id="job-1"),
    "list_job_templates": ListJobTemplates(),
    "save_job_template": SaveJobTemplate(
        name="Inbox digest",
        instruction="summarize the inbox",
        cadence="weekdays at 7:45",
        deliver_to="window",
        grace="2 hours",
        retries=1,
        retry_delay="10 minutes",
        max_run="20 minutes",
        preset="vllm",
        engine="native",
        workspace="/home/user/project",
    ),
    "delete_job_template": DeleteJobTemplate(template_id="tmpl-1"),
    "get_session_job_source": GetSessionJobSource(session_id="sess-1"),
    "job_list": JobList(
        jobs=[
            JobEntry(
                id="job-1",
                workspace="/home/user/project",
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                paused=False,
                preset="vllm",
                engine="native",
                grace=7200,
                retries=2,
                retry_delay=600,
                attempt=0,
                max_run=1200,
                then="job-2",
                skip_calendar="/home/user/holidays.ics",
                skip_match="holiday|PTO|OOO",
                # TD-3807: a real receipt, not nulls — an all-null sample
                # would not prove the TypeScript side accepts the values.
                last_run="2026-08-21T15:00:00+00:00",
                last_status="ok",
                last_summary="3 threads need a reply",
                last_session_id="sess-1",
                running=False,
            )
        ],
    ),
    "job_runs": JobRuns(
        job_id="job-1",
        runs=[
            JobRunEntry(
                started_at="2026-08-21T15:00:00+00:00",
                scheduled_for="2026-08-21T12:00:00+00:00",
                trigger="schedule",
                status="ok",
                summary="3 threads need a reply",
                session_id="sess-1",
                attempt=2,
                attempts=3,
            ),
            JobRunEntry(
                started_at="2026-08-21T22:45:00+00:00",
                scheduled_for="2026-08-21T12:45:00+00:00",
                trigger="schedule",
                status="missed",
                summary="Skipped the 7:45 AM run — 10 h late",
                session_id=None,
            ),
            JobRunEntry(
                started_at="2026-08-21T23:00:00+00:00",
                scheduled_for="2026-08-21T19:00:00+00:00",
                trigger="schedule",
                status="waiting",
                summary="Run `echo hi`",
                session_id="sess-park",
            ),
            JobRunEntry(
                started_at="2026-08-21T23:30:00+00:00",
                scheduled_for=None,
                trigger="chained",
                status="ok",
                summary="draft ready",
                session_id="sess-chain",
                note="after job-1",
            ),
            JobRunEntry(
                started_at="2026-12-25T15:00:00+00:00",
                scheduled_for="2026-12-25T13:00:00+00:00",
                trigger="schedule",
                status="skipped",
                summary="calendar",
                session_id=None,
            ),
        ],
    ),
    "job_templates": JobTemplates(
        templates=[
            JobTemplateEntry(
                id="weekday-morning-digest",
                name="Weekday morning digest",
                builtin=True,
                instruction="",
                cadence="weekdays at 7:45",
                next_run=None,
                deliver_to="window",
                grace=7200,
                retries=1,
                retry_delay=600,
                max_run=None,
                preset=None,
                engine=None,
                workspace=None,
            ),
            JobTemplateEntry(
                id="tmpl-1",
                name="Inbox digest",
                builtin=False,
                instruction="summarize the inbox",
                cadence=None,
                next_run="tomorrow at 9:00",
                deliver_to="window",
                grace=None,
                retries=0,
                retry_delay=None,
                max_run=1200,
                preset="vllm",
                engine="native",
                workspace="/home/user/project",
            ),
        ]
    ),
    "session_job_source": SessionJobSource(
        session_id="sess-1",
        workspace="/home/user/project",
        preset="vllm",
        engine="grok",
        instruction="summarize the inbox",
    ),
    "job_draft": JobDraftReply(
        ok=True,
        workspace="/ws/proj",
        instruction="summarize the inbox",
        cadence="every 2 hours",
        deliver_to="slack",
        paused=False,
    ),
    "grok_commands": GrokCommands(
        session_id="sess-1",
        commands=[GrokCommand(name="compact", description="Compress history")],
        seq=2,
    ),
    "grok_plan": GrokPlan(
        session_id="sess-1",
        markdown="# Plan\n\nDo the thing.",
        entries=[GrokPlanEntry(content="Step one", status="pending")],
        seq=3,
    ),
    "grok_mode": GrokMode(
        session_id="sess-1",
        mode="plan",
        modes=["default", "plan", "yolo"],
        model="grok-4.6",
        seq=4,
    ),
    "grok_preview": GrokPreview(
        session_id="sess-1",
        kind="image",
        path="/tmp/out.png",
        title="out.png",
        seq=5,
    ),
    "grok_session_list": GrokSessionList(
        sessions=[GrokSessionEntry(id="abc", title="Fix login", cwd="/tmp/proj", updated_at="1")]
    ),
    "transcript": Transcript(ok=True, text="hello from the mic", detail=""),
    "email_test_result": EmailTestResult(
        ok=False, error_class="SMTPException", message="auth failed"
    ),
    "github_status": GithubStatus(
        enabled=True,
        connected=False,
        phase="pending",
        user_code="ABCD-1234",
        verification_uri="https://example.test/login/device",
        login=None,
        message="",
    ),
    "microsoft_status": MicrosoftStatus(
        enabled=True,
        connected=True,
        phase="connected",
        account="owner@example.com",
        message="",
    ),
    "google_status": GoogleStatus(
        enabled=True,
        connected=True,
        phase="connected",
        account="owner@example.com",
        message="",
        client_secret_stored=True,
    ),
    "grok_extensions": GrokExtensions(
        items=[GrokExtension(kind="skill", name="review", detail="user")]
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
    # hello_ack is built as a plain dict, not a Pydantic model. machine_id
    # and desktop_name say which computer answered (TD-5015).
    fixtures["hello_ack"] = json.loads(
        build_hello_ack(machine_id="mid_0123456789abcdef", desktop_name="desk")
    )
    # pair_ok too (TD-5014). The key is a placeholder, never a real one.
    fixtures["pair_ok"] = json.loads(
        build_pair_ok("dev_0123456789ab", "Pixel", "0" * 64, "desk", "mid_0123456789abcdef")
    )
    # Not a message: the verbs a hub forwards for bridged ids (TD-5015). A
    # UI test pins its own list against this one.
    fixtures["forwarded_verbs"] = sorted(FORWARDED_VERBS)
    output_path.write_text(json.dumps(fixtures, indent=2))
    print(f"Wrote {len(fixtures)} fixtures to {output_path}")


if __name__ == "__main__":
    main()
