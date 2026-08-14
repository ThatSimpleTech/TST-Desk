"""Typed protocol messages exchanged between the shell and the daemon.

Every message has a discriminated `type` field. Client→daemon messages
are `ClientMessage`; daemon→client events are `DaemonEvent` and carry a
monotonic `seq` scoped to the session.

Field naming is snake_case on the wire in both Python and TypeScript
(see DECISIONS.md 2026-08-12 TD-204 §1).
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .logging import redact_secrets
from .router import TierName

# Current protocol version
PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1

# Token length in bytes (64 hex chars)
_TOKEN_BYTES = 32


class HandshakeError(Exception):
    """Raised when a handshake fails. Carries a typed error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ── Handshake helpers ──────────────────────────────────────────────────


def generate_token() -> str:
    """Generate a random hex auth token."""
    return secrets.token_hex(_TOKEN_BYTES)


def validate_version(version: int) -> None:
    """Check that the client's protocol version is compatible.

    Raises:
        HandshakeError: If the version is incompatible.
    """
    if version < MIN_PROTOCOL_VERSION:
        raise HandshakeError(
            "version_unsupported",
            f"Protocol version {version} is too old. "
            f"Minimum supported: {MIN_PROTOCOL_VERSION}. "
            f"Current: {PROTOCOL_VERSION}. "
            f"Please upgrade your client.",
        )
    if version > PROTOCOL_VERSION:
        raise HandshakeError(
            "version_unsupported",
            f"Protocol version {version} is too new. "
            f"Server supports: {PROTOCOL_VERSION}. "
            f"Please upgrade the daemon.",
        )


def validate_token(provided: str, expected: str) -> None:
    """Check that the client's token matches the server's.

    Raises:
        HandshakeError: If the token is invalid.
    """
    if provided != expected:
        raise HandshakeError(
            "auth_failed",
            "Invalid auth token. Check the token in the port file and try again.",
        )


# ── Base classes ───────────────────────────────────────────────────────


class ClientMessage(BaseModel):
    """Base class for client→daemon messages."""

    type: str


class DaemonEvent(BaseModel):
    """Base class for daemon→client events. Carries a session-scoped seq."""

    type: str
    seq: int = Field(gt=0, description="Monotonic per-session event sequence")


# ── Client → Daemon ────────────────────────────────────────────────────


class Hello(ClientMessage):
    """Opening handshake: token + protocol version."""

    type: Literal["hello"] = "hello"
    token: str = Field(min_length=1)
    version: int = Field(ge=0, description="Validated by validate_hello()")


class OpenWorkspace(ClientMessage):
    """Open a workspace directory for a new session."""

    type: Literal["open_workspace"] = "open_workspace"
    path: str


class UserMessage(ClientMessage):
    """A user message to the current session."""

    type: Literal["user_message"] = "user_message"
    session_id: str
    content: str


class Approve(ClientMessage):
    """Approve a pending tool call."""

    type: Literal["approve"] = "approve"
    session_id: str
    tool_call_id: str


class Deny(ClientMessage):
    """Deny a pending tool call."""

    type: Literal["deny"] = "deny"
    session_id: str
    tool_call_id: str
    reason: str | None = None


class AlwaysAllow(ClientMessage):
    """Always-allow a pending tool call (TD-803).

    Writes the narrowest policy rule for the call, then resolves the pending
    approval as approved.  Rejected with ``class_c_not_always_allowable``
    when the call is a class-C decision.
    """

    type: Literal["always_allow"] = "always_allow"
    session_id: str
    tool_call_id: str


class ListPolicyRules(ClientMessage):
    """List the workspace's saved policy rules (TD-803 settings)."""

    type: Literal["list_policy_rules"] = "list_policy_rules"
    session_id: str


class RevokePolicyRule(ClientMessage):
    """Remove a saved policy rule by ``(tool, args)`` (TD-803 settings)."""

    type: Literal["revoke_policy_rule"] = "revoke_policy_rule"
    session_id: str
    tool: str
    args: str


class Resume(ClientMessage):
    """Resume a session paused at a declared cap (TD-707).

    The user raises the cap (editing ``.tst/config.yaml``) and resumes;
    the daemon reloads the boundary and the loop re-checks caps before
    its next model call.
    """

    type: Literal["resume"] = "resume"
    session_id: str


class Cancel(ClientMessage):
    """Cancel a running session."""

    type: Literal["cancel"] = "cancel"
    session_id: str


class Attach(ClientMessage):
    """Attach to a session, replaying events from `from_seq`."""

    type: Literal["attach"] = "attach"
    session_id: str
    from_seq: int = Field(default=1, ge=1)


class Detach(ClientMessage):
    """Detach from a session, stopping the live stream."""

    type: Literal["detach"] = "detach"
    session_id: str


class SetTier(ClientMessage):
    """Override the active model tier for a session."""

    type: Literal["set_tier"] = "set_tier"
    session_id: str
    tier: Literal["brain", "worker", "validator"]


class GetInstructionStack(ClientMessage):
    """Request the current instruction stack for a session."""

    type: Literal["get_instruction_stack"] = "get_instruction_stack"
    session_id: str


class Shutdown(ClientMessage):
    """Ask the daemon to shut down cleanly (sent by the supervising host)."""

    type: Literal["shutdown"] = "shutdown"


class ListSessions(ClientMessage):
    """Request the current session list (id, workspace, state, timestamps)."""

    type: Literal["list_sessions"] = "list_sessions"


class NewSession(ClientMessage):
    """Create a fresh session in an existing session's workspace (TD-1701).

    The sidebar's New action anchors on the attached session rather than a
    path: the registry is the source of truth for which workspace a session
    belongs to, so the client never carries a path it may have lost across
    a restart.  The daemon wires the new session exactly like
    ``open_workspace`` and replies with its first event (``session_state``).
    """

    type: Literal["new_session"] = "new_session"
    session_id: str


class GetSetupState(ClientMessage):
    """Request the onboarding setup state (TD-1101 first-run wizard).

    The daemon answers with a ``setup_state`` event: whether an API key is
    stored, the declared presets, and the active one.
    """

    type: Literal["get_setup_state"] = "get_setup_state"


class SetApiKey(ClientMessage):
    """Store an API key in the OS keychain (TD-1101).

    The key never appears in any event, log, or audit record — the ack is a
    refreshed ``setup_state`` event whose ``has_api_key`` flips true.
    """

    type: Literal["set_api_key"] = "set_api_key"
    api_key: str = Field(min_length=1)


class ValidateApiKey(ClientMessage):
    """Probe the stored key with one cheap live call (TD-1101).

    The daemon answers with ``api_key_validated``; the key itself never
    leaves the keychain except inside the provider client's auth header.
    """

    type: Literal["validate_api_key"] = "validate_api_key"


class SetPreset(ClientMessage):
    """Pick the active model preset (TD-1101); applied to new sessions."""

    type: Literal["set_preset"] = "set_preset"
    name: str = Field(min_length=1)


class RunDiagnostics(ClientMessage):
    """Ask the daemon to run the doctor checks (TD-1104 diagnostics).

    The daemon answers with a single ``diagnostics_report`` once every
    check completes — including the one-token provider probe, so the
    reply can take a second or two.
    """

    type: Literal["run_diagnostics"] = "run_diagnostics"


# ── Daemon → Client ────────────────────────────────────────────────────


class Ready(DaemonEvent):
    """Sent after a successful handshake."""

    type: Literal["ready"] = "ready"
    seq: int = 1  # `ready` is connection-scoped, not session-scoped
    version: str
    protocol_version: int


class SessionState(DaemonEvent):
    """Session state transition."""

    type: Literal["session_state"] = "session_state"
    session_id: str
    state: Literal[
        "idle",
        "running",
        "awaiting_approval",
        "paused",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]
    reason: str | None = None


class AssistantDelta(DaemonEvent):
    """A streamed chunk of assistant output."""

    type: Literal["assistant_delta"] = "assistant_delta"
    session_id: str
    delta: str


class ToolCall(DaemonEvent):
    """A tool call about to be executed."""

    type: Literal["tool_call"] = "tool_call"
    session_id: str
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    decision_class: Literal["A", "B", "C"] | None = None


class ToolResult(DaemonEvent):
    """The result of a tool call."""

    type: Literal["tool_result"] = "tool_result"
    session_id: str
    tool_call_id: str
    status: Literal["success", "error"]
    output: str
    truncated: bool = False
    # Machine-readable code for ``error`` status ("approval_denied",
    # "policy_denied", "boundary_refusal", …).  Carried so the UI can
    # distinguish "denied by user" from a generic handler error (TD-1007).
    error_code: str | None = None
    # Unified diff of what a write changed (TD-604), for display.
    diff: str | None = None


class ShellOutput(DaemonEvent):
    """A streamed chunk of a shell command's stdout or stderr (TD-605).

    Emitted by the shell handler as output arrives, so the timeline shows
    command output live rather than one block at the end.
    """

    type: Literal["shell_output"] = "shell_output"
    session_id: str
    tool_call_id: str
    stream: Literal["stdout", "stderr"]
    chunk: str


class PolicyRuleSummary(BaseModel):
    """A saved policy rule, surfaced to clients (TD-803).

    ``(tool, args)`` is the identity used for individual revocation in
    settings.
    """

    tool: str
    args: str
    effect: Literal["auto", "ask", "never"]


class ApprovalRequest(DaemonEvent):
    """A request for user approval of a tool call (TD-802).

    ``summary`` is the human-readable action ("Run `npm test`");
    ``reason`` is why approval is required ("decision class B requires
    approval", "policy rule `shell: rm *` → ask").

    ``proposed_always_allow`` (TD-803) is the rule that "always allow in
    this workspace" would write, or ``None`` when the call is a class-C
    decision that can never be always-allowed.  The client shows it before
    the user commits to saving it.
    """

    type: Literal["approval_request"] = "approval_request"
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    decision_class: Literal["A", "B", "C"]
    summary: str
    reason: str
    proposed_always_allow: PolicyRuleSummary | None = None


class DecisionLogged(DaemonEvent):
    """A decision appended to the autonomy ledger."""

    type: Literal["decision_logged"] = "decision_logged"
    session_id: str
    decision_class: Literal["A", "B", "C"]
    what: str
    why: str
    # The revertable commit SHA; ``None`` for an uncommitted Class B
    # entry (Class A always names one — TD-704 AC 3).
    commit: str | None = None


class CheckpointNotice(DaemonEvent):
    """A one-time checkpoint degradation notice (TD-705).

    Emitted at most once per code per session — e.g. the workspace is
    not a git repository, or has pre-existing uncommitted changes.
    """

    type: Literal["checkpoint_notice"] = "checkpoint_notice"
    session_id: str
    code: str
    message: str


class CostUpdate(DaemonEvent):
    """Accrued cost for the session."""

    type: Literal["cost_update"] = "cost_update"
    session_id: str
    turn_cost: float = Field(ge=0)
    session_cost: float = Field(ge=0)
    total_cost: float = Field(ge=0)
    # Decision-classifier worker calls (TD-703), tracked separately from
    # main-loop cost.
    classifier_cost: float = Field(default=0.0, ge=0)
    # Per-tier session spend (TD-1006) — keys are tier names that spent.
    # Powers the title bar's hover breakdown. Classifier calls go to
    # classifier_cost, not here.
    cost_by_tier: dict[str, float] = Field(default_factory=dict)


class TierState(DaemonEvent):
    """Active model tier and configured slugs (TD-1006).

    Emitted when a session opens, when a ``set_tier`` override lands, and
    whenever the router changes tier between turns (lead-turns handoff,
    failure escalation). ``tier`` is what handles the next turn;
    ``override`` is the pinned override when the user picked one.
    """

    type: Literal["tier_state"] = "tier_state"
    session_id: str
    tier: TierName
    override: TierName | None = None
    # tier name → configured model slug, "brain"/"worker"/"validator".
    model_slugs: dict[str, str] = Field(default_factory=dict)


class BoundaryUpdate(DaemonEvent):
    """The workspace boundary, emitted when a session opens (TD-706).

    The UI shows the wall (TD-1006); the daemon resolves it from
    ``.tst/config.yaml`` or defaults.
    """

    type: Literal["boundary_update"] = "boundary_update"
    session_id: str
    writable_paths: list[str]
    allowed_commands: list[str]
    network: str | list[str]
    spend_usd: float = Field(ge=0)
    wall_clock_hours: float = Field(ge=0)
    max_iterations: int = Field(ge=1)
    # Config file path, "defaults", or "defaults — invalid config (…)".
    source: str


class TurnComplete(DaemonEvent):
    """Summary of a completed turn.

    ``failed`` marks a turn that ended on a provider/keychain error rather
    than a model response; ``error_code`` carries the typed cause (e.g.
    ``auth_failed``, ``rate_limited``, ``missing_api_key``) so clients can
    show tailored copy (TD-1008).
    """

    type: Literal["turn_complete"] = "turn_complete"
    session_id: str
    tokens: int = Field(ge=0)
    cost: float = Field(ge=0)
    tier: Literal["brain", "worker", "validator"]
    duration: float = Field(ge=0)
    failed: bool = False
    error_code: str | None = None


class ContextCompacted(DaemonEvent):
    """Emitted when older conversation turns are compacted to stay inside
    the tier's context window (TD-405). Never silent: the timeline shows
    exactly what compaction did."""

    type: Literal["context_compacted"] = "context_compacted"
    session_id: str
    dropped_messages: int = Field(ge=0)
    kept_messages: int = Field(ge=0)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)


class SteeringReloaded(DaemonEvent):
    """Emitted when steering files are re-resolved after a detected change."""

    type: Literal["steering_reloaded"] = "steering_reloaded"
    session_id: str
    prefix_hash: str
    prefix_tokens: int = Field(ge=0)
    source_count: int = Field(ge=0)


class TierSwitched(DaemonEvent):
    """Emitted when a session's active tier is overridden via ``set_tier``.

    The timeline shows this as an explicit entry so a manual routing change
    is visible alongside the automatic tier decisions (TD-1005). ``previous``
    records the tier before the override so the entry reads as a transition.
    """

    type: Literal["tier_switched"] = "tier_switched"
    session_id: str
    tier: Literal["brain", "worker", "validator"]
    previous: Literal["brain", "worker", "validator"] | None = None


class ImportedFile(BaseModel):
    """One resolved ``@path`` import, flattened with its nesting depth.

    ``depth`` 1 is a direct import of the source carrying it; deeper
    values nest under the preceding entry one level up (TD-1201 renders
    the tree from this). ``issue`` is set when resolution failed (missing
    file, cycle, depth exceeded) so the panel can flag it in place.
    """

    path: str
    depth: int = Field(ge=1)
    issue: str | None = None


class InstructionStackEntry(BaseModel):
    """One resolved steering source in the instruction stack."""

    path: str
    precedence: str
    active: bool = True
    tokens: int = Field(ge=0)
    token_method: str
    warnings: list[str] = Field(default_factory=list)
    subtree: str | None = None
    is_fallback: bool = False
    # Path of the ``CLAUDE.md`` this source shadows (it is an AGENTS.md at
    # the same location), when applicable (TD-1201).
    shadowed_path: str | None = None
    # ``appliesTo`` globs from rule-file frontmatter — what a path-scoped
    # rule matched against (TD-1201).
    applies_to: list[str] | None = None
    imports: list[ImportedFile] = Field(default_factory=list)


class InstructionStack(DaemonEvent):
    """Response to ``get_instruction_stack``: the resolved stack with counts."""

    type: Literal["instruction_stack"] = "instruction_stack"
    session_id: str
    sources: list[InstructionStackEntry] = Field(default_factory=list)
    total_tokens: int = Field(ge=0)
    token_method: str
    # Cached prompt tokens observed on the most recent provider call
    # (TD-1201's "is the block currently cached"). ``None`` before the
    # first turn — cache state is a provider-side fact, unknown until one.
    last_cached_tokens: int | None = None


class SessionSummary(BaseModel):
    """One entry in a ``session_list`` response."""

    session_id: str
    workspace_path: str
    state: Literal[
        "idle",
        "running",
        "awaiting_approval",
        "paused",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]
    created_at: str
    updated_at: str
    event_count: int = Field(ge=0)


class SessionList(DaemonEvent):
    """Response to ``list_sessions``: the current session list.

    Connection-scoped (like ``ready``), so its seq is fixed at 1 rather than
    riding the session log.
    """

    type: Literal["session_list"] = "session_list"
    seq: int = 1
    sessions: list[SessionSummary] = Field(default_factory=list)


class PolicyRules(DaemonEvent):
    """Response to ``list_policy_rules`` / ``revoke_policy_rule`` (TD-803).

    Carries the workspace's saved policy rules so the settings surface can
    list and revoke them individually.
    """

    type: Literal["policy_rules"] = "policy_rules"
    seq: int = 1
    rules: list[PolicyRuleSummary] = Field(default_factory=list)


class SetupState(DaemonEvent):
    """Response to ``get_setup_state``; also the ack for ``set_api_key`` and
    ``set_preset`` (TD-1101).

    Connection-scoped (like ``session_list``), so its seq is fixed at 1.
    ``has_api_key`` is the first-run signal: no key stored means the wizard
    shows.
    """

    type: Literal["setup_state"] = "setup_state"
    seq: int = 1
    has_api_key: bool
    presets: list[str] = Field(default_factory=list)
    active_preset: str


class ApiKeyValidated(DaemonEvent):
    """Response to ``validate_api_key`` (TD-1101).

    ``detail`` carries the actionable failure text on failure (already
    redacted at the provider boundary); on success it says which keychain
    account was checked, never the key.
    """

    type: Literal["api_key_validated"] = "api_key_validated"
    seq: int = 1
    ok: bool
    detail: str


class DiagnosticCheck(BaseModel):
    """One doctor check (TD-1104).

    ``skip`` means the check did not apply (no key to validate, no
    workspace open) — it is not a failure and should not alarm.  ``fix``
    is the concrete remedy; present exactly when status is ``fail``.
    """

    name: str
    status: Literal["ok", "fail", "skip"]
    detail: str
    fix: str | None = None


class DiagnosticsReport(DaemonEvent):
    """Doctor results (TD-1104), answering ``run_diagnostics``.

    Connection-scoped like ``setup_state``: the report describes the
    machine, not a session.
    """

    type: Literal["diagnostics_report"] = "diagnostics_report"
    seq: int = 1
    checks: list[DiagnosticCheck] = Field(default_factory=list)


class Error(DaemonEvent):
    """A typed error, usually in response to a bad message."""

    type: Literal["error"] = "error"
    session_id: str | None = None
    code: str
    message: str


# ── Discriminated unions ───────────────────────────────────────────────

ClientMessageT = Annotated[
    Hello
    | OpenWorkspace
    | UserMessage
    | Approve
    | Deny
    | AlwaysAllow
    | ListPolicyRules
    | RevokePolicyRule
    | Resume
    | Cancel
    | Attach
    | Detach
    | SetTier
    | GetInstructionStack
    | Shutdown
    | ListSessions
    | NewSession
    | GetSetupState
    | SetApiKey
    | ValidateApiKey
    | SetPreset
    | RunDiagnostics,
    Field(discriminator="type"),
]

DaemonEventT = Annotated[
    Ready
    | SessionState
    | AssistantDelta
    | ToolCall
    | ToolResult
    | ShellOutput
    | ApprovalRequest
    | DecisionLogged
    | CheckpointNotice
    | CostUpdate
    | BoundaryUpdate
    | TurnComplete
    | TierState
    | ContextCompacted
    | SteeringReloaded
    | TierSwitched
    | InstructionStack
    | SessionList
    | PolicyRules
    | SetupState
    | ApiKeyValidated
    | DiagnosticsReport
    | Error,
    Field(discriminator="type"),
]

_client_message_adapter: TypeAdapter[ClientMessageT] = TypeAdapter(ClientMessageT)
_daemon_event_adapter: TypeAdapter[DaemonEventT] = TypeAdapter(DaemonEventT)

# Known message types for explicit unknown-type detection
_KNOWN_CLIENT_TYPES = frozenset(
    {
        "hello",
        "open_workspace",
        "user_message",
        "approve",
        "deny",
        "always_allow",
        "list_policy_rules",
        "revoke_policy_rule",
        "resume",
        "cancel",
        "attach",
        "detach",
        "set_tier",
        "get_instruction_stack",
        "shutdown",
        "list_sessions",
        "new_session",
        "get_setup_state",
        "set_api_key",
        "validate_api_key",
        "set_preset",
        "run_diagnostics",
    }
)
_KNOWN_EVENT_TYPES = frozenset(
    {
        "ready",
        "session_state",
        "assistant_delta",
        "tool_call",
        "tool_result",
        "shell_output",
        "approval_request",
        "decision_logged",
        "checkpoint_notice",
        "cost_update",
        "boundary_update",
        "turn_complete",
        "tier_state",
        "context_compacted",
        "steering_reloaded",
        "tier_switched",
        "instruction_stack",
        "session_list",
        "policy_rules",
        "setup_state",
        "api_key_validated",
        "diagnostics_report",
        "error",
    }
)


class UnknownMessageTypeError(HandshakeError):
    """Raised when a message has an unrecognized `type`."""

    def __init__(self, message_type: str) -> None:
        super().__init__(
            "unknown_message",
            f"Unknown message type {message_type!r}. Check the protocol version and message name.",
        )


def _check_known_type(data: dict[str, Any], known_types: frozenset[str]) -> None:
    """Raise UnknownMessageTypeError if the type field is not in known_types."""
    msg_type = data.get("type")
    if isinstance(msg_type, str) and msg_type not in known_types:
        raise UnknownMessageTypeError(msg_type)


def parse_client_message(raw: str) -> ClientMessageT:
    """Parse a raw JSON string into a typed client message.

    Raises:
        HandshakeError: If the message is malformed or has an unknown type.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HandshakeError("bad_request", f"Invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise HandshakeError("bad_request", "Message must be a JSON object")
    if "type" not in data or not isinstance(data["type"], str):
        raise HandshakeError("bad_request", "Message missing string 'type' field")

    _check_known_type(data, _KNOWN_CLIENT_TYPES)

    try:
        return _client_message_adapter.validate_python(data)
    except ValueError as e:
        raise HandshakeError("bad_request", str(e)) from e


def parse_daemon_event(raw: str) -> DaemonEventT:
    """Parse a raw JSON string into a typed daemon event.

    Raises:
        HandshakeError: If the event is malformed or has an unknown type.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HandshakeError("bad_request", f"Invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise HandshakeError("bad_request", "Event must be a JSON object")
    if "type" not in data or not isinstance(data["type"], str):
        raise HandshakeError("bad_request", "Event missing string 'type' field")

    _check_known_type(data, _KNOWN_EVENT_TYPES)

    try:
        return _daemon_event_adapter.validate_python(data)
    except ValueError as e:
        raise HandshakeError("bad_request", str(e)) from e


def parse_hello(raw: str) -> Hello:
    """Parse a raw JSON string into a Hello message (for the handshake)."""
    return parse_client_message(raw)  # type: ignore[return-value]


def build_hello_ack() -> str:
    """Build the server's handshake acknowledgement."""
    return json.dumps({"type": "hello_ack", "version": PROTOCOL_VERSION})


def build_error(code: str, message: str) -> str:
    """Build a typed error message.

    The message passes through the shared redaction chokepoint (TD-1405):
    this envelope bypasses the event log — it is written straight to the
    socket — so it must scrub here rather than rely on ``event_log.add``.
    """
    return json.dumps({"type": "error", "code": code, "message": redact_secrets(message)})


def validate_hello(hello: Hello) -> None:
    """Validate the version and token of a parsed hello message."""
    validate_version(hello.version)
