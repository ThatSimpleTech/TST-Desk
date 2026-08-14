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
    AlwaysAllow,
    ApprovalRequest,
    Approve,
    AssistantDelta,
    Attach,
    BoundaryUpdate,
    Cancel,
    CheckpointNotice,
    CostUpdate,
    DecisionLogged,
    Deny,
    Detach,
    Error,
    GetInstructionStack,
    Hello,
    InstructionStack,
    ListPolicyRules,
    ListSessions,
    OpenWorkspace,
    PolicyRules,
    PolicyRuleSummary,
    Ready,
    Resume,
    RevokePolicyRule,
    SessionList,
    SessionState,
    SetTier,
    ShellOutput,
    Shutdown,
    SteeringReloaded,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserMessage,
)

FIXTURES = {
    # Client messages
    "hello": Hello(token="test-token-abc", version=1),
    "open_workspace": OpenWorkspace(path="/home/user/project"),
    "user_message": UserMessage(session_id="sess-1", content="hello world"),
    "approve": Approve(session_id="sess-1", tool_call_id="tc-1"),
    "deny": Deny(session_id="sess-1", tool_call_id="tc-1", reason="not safe"),
    "deny_no_reason": Deny(session_id="sess-1", tool_call_id="tc-1"),
    "always_allow": AlwaysAllow(session_id="sess-1", tool_call_id="tc-1"),
    "list_policy_rules": ListPolicyRules(session_id="sess-1"),
    "revoke_policy_rule": RevokePolicyRule(session_id="sess-1", tool="shell", args="rm *"),
    "resume": Resume(session_id="sess-1"),
    "cancel": Cancel(session_id="sess-1"),
    "attach": Attach(session_id="sess-1", from_seq=5),
    "detach": Detach(session_id="sess-1"),
    "set_tier": SetTier(session_id="sess-1", tier="brain"),
    "get_instruction_stack": GetInstructionStack(session_id="sess-1"),
    "shutdown": Shutdown(),
    "list_sessions": ListSessions(),
    # Daemon events
    "ready": Ready(version="0.1.0", protocol_version=PROTOCOL_VERSION),
    "session_state": SessionState(session_id="sess-1", state="running", seq=2),
    "session_state_paused": SessionState(
        session_id="sess-1",
        state="paused",
        reason="spend cap exceeded: $0.0205 >= $0.01",
        seq=2,
    ),
    "assistant_delta": AssistantDelta(session_id="sess-1", delta="Hello ", seq=3),
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
        source_count=3,
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
    "session_list": SessionList(
        sessions=[
            {
                "session_id": "sess-1",
                "workspace_path": "/home/user/project",
                "state": "interrupted",
                "created_at": "2026-08-13T10:00:00Z",
                "updated_at": "2026-08-13T10:00:00Z",
                "event_count": 0,
            }
        ]
    ),
    "policy_rules": PolicyRules(
        rules=[
            PolicyRuleSummary(tool="shell", args="npm test", effect="auto"),
            PolicyRuleSummary(tool="fs_*", args="src/**", effect="ask"),
        ]
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
