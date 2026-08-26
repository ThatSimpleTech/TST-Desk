"""Typed protocol messages exchanged between the shell and the daemon.

Every message has a discriminated `type` field. Client→daemon messages
are `ClientMessage`; daemon→client events are `DaemonEvent` and carry a
monotonic `seq` scoped to the session.

Field naming is snake_case on the wire in both Python and TypeScript
(see DECISIONS.md 2026-08-12 TD-204 §1).
"""

from __future__ import annotations

import hmac
import json
import secrets
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .attachments import AttachmentLimits
from .autonomy.charter import Charter
from .logging import redact_secrets, redact_structure
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

    Compared in constant time on UTF-8 bytes: the token is the only
    credential on the loopback socket, so a timing side channel is a
    remote-local concern, but ``!=`` on a secret is never the right
    habit. Bytes comparison also keeps a non-ASCII ``provided`` from
    raising ``TypeError`` instead of failing the handshake cleanly.

    Raises:
        HandshakeError: If the token is invalid.
    """
    if not hmac.compare_digest(provided.encode(), expected.encode()):
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


class Attachment(BaseModel):
    """One text file riding along with a ``user_message`` (TD-1709).

    ``content_b64`` carries the file's *bytes*, not a decode the client made
    first, so the daemon answers "is this text?" itself — see
    ``attachments.py`` for why that distinction is the whole gate.  Nothing
    else is declared: a client-stated size or mime type would be a fact the
    daemon has to re-derive anyway, and two sources for one fact is how they
    start disagreeing.
    """

    name: str = Field(min_length=1, max_length=255)
    content_b64: str = ""


class UserMessage(ClientMessage):
    """A user message to the current session.

    ``attachments`` is additive with a default (TD-1709): a client that never
    sends one behaves exactly as it did.  The daemon decodes and vets them
    against the workspace's ``attachments`` caps before the message is
    enqueued, and refuses the whole message if any one fails.
    """

    type: Literal["user_message"] = "user_message"
    session_id: str
    content: str
    attachments: list[Attachment] = Field(default_factory=list)


class ForkFrom(ClientMessage):
    """Replace a past user turn and fork the conversation from there (TD-1708).

    ``user_index`` is 0-based among user turns.  Refused while a turn is
    in flight.  The daemon answers with ``conversation_reset``.
    """

    type: Literal["fork_from"] = "fork_from"
    session_id: str
    user_index: int
    content: str


class SetBranch(ClientMessage):
    """Switch to another sibling at a forked user turn (TD-1708)."""

    type: Literal["set_branch"] = "set_branch"
    session_id: str
    user_index: int
    sibling_index: int


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


class SetSkipAllApprovals(ClientMessage):
    """Turn skip-all approvals on or off (TD-804).

    Machine-wide, no session.  The daemon answers with a refreshed
    ``setup_state`` so the toggle is honest after restart.  Class C and
    ``never`` rules are unaffected.
    """

    type: Literal["set_skip_all_approvals"] = "set_skip_all_approvals"
    enabled: bool


class SetWorkspacePin(ClientMessage):
    """Pin or unpin a workspace on the Projects list (TD-2806)."""

    type: Literal["set_workspace_pin"] = "set_workspace_pin"
    path: str = Field(min_length=1)
    pinned: bool


class SetLoadGlobalMemory(ClientMessage):
    """Turn global memory on or off (TD-2603).

    Machine-wide, no session. Files stay at ``~/.tstdesk/memory/``.
    The daemon answers with ``setup_state``. Default off.
    """

    type: Literal["set_load_global_memory"] = "set_load_global_memory"
    enabled: bool


class SetCoworker(ClientMessage):
    """Turn coworker mode on or off (TD-2905).

    Machine-wide, no session. Persists ``{user_data_dir}/coworker.yaml``.
    The daemon answers with ``setup_state``. Default on.
    """

    type: Literal["set_coworker"] = "set_coworker"
    enabled: bool


class SetCuIndicators(ClientMessage):
    """Computer-use glow / agent cursor / real-display overlay (TD-3402).

    Machine-wide, no session. Persists ``{user_data_dir}/cu-indicators.yaml``.
    The daemon answers with ``setup_state``. Glow and cursor default on;
    the real-display overlay defaults off.
    """

    type: Literal["set_cu_indicators"] = "set_cu_indicators"
    glow: bool
    agent_cursor: bool
    show_on_real_display: bool


class SetRemoteAttach(ClientMessage):
    """Turn Tailscale remote attach on or off (TD-3603).

    Machine-wide, no session. Persists ``{user_data_dir}/remote-attach.yaml``.
    On sets ``remote.bind`` to the last-known Tailscale target (default
    ``tailscale0``) and starts the extra listener. Off clears the bind
    and drops that listener; loopback stays. The daemon answers with
    ``setup_state``. Default off. Never carries a token.
    """

    type: Literal["set_remote_attach"] = "set_remote_attach"
    enabled: bool


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


class RunVerify(ClientMessage):
    """Confirm a pending interactive verify (TD-4204 ask mode)."""

    type: Literal["run_verify"] = "run_verify"
    session_id: str


class DenyVerify(ClientMessage):
    """Skip a pending interactive verify (TD-4204 ask mode)."""

    type: Literal["deny_verify"] = "deny_verify"
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


class SetPlan(ClientMessage):
    """Turn plan mode (brain lock) on or off (TD-4603).

    While on, every completion is ``brain`` and ``set_tier`` to
    worker or validator is refused. Not a plan document.
    """

    type: Literal["set_plan"] = "set_plan"
    session_id: str
    on: bool


class SetSessionPreset(ClientMessage):
    """Retarget this session at a catalog preset (TD-1721).

    Settings' ``set_preset`` stays the machine-wide default for new
    sessions. This verb writes the preset *name* onto the session
    record — not a forked config tree — and is refused while a turn
    is running. Acked with ``session_list``; the title bar follows
    the ``tier_state`` the session log also emits.
    """

    type: Literal["set_session_preset"] = "set_session_preset"
    session_id: str
    name: str = Field(min_length=1)


class GetInstructionStack(ClientMessage):
    """Request the current instruction stack for a session."""

    type: Literal["get_instruction_stack"] = "get_instruction_stack"
    session_id: str


class ListInstructions(ClientMessage):
    """List a workspace's Instructions files (TD-2802). Human path."""

    type: Literal["list_instructions"] = "list_instructions"
    workspace_path: str


class ListCommands(ClientMessage):
    """List a workspace's slash commands (TD-4501). Human path."""

    type: Literal["list_commands"] = "list_commands"
    workspace_path: str


class ListMemory(ClientMessage):
    """List a workspace's Memory files (TD-2601). Human path."""

    type: Literal["list_memory"] = "list_memory"
    workspace_path: str


class SaveMemory(ClientMessage):
    """Save an edit from the Memory pane (TD-2602). Human path, never a tool."""

    type: Literal["save_memory"] = "save_memory"
    workspace_path: str
    path: str = Field(min_length=1)
    content: str


class CreateRule(ClientMessage):
    """Create a ``.tst/rules/`` file (TD-2802). Human path, never a tool."""

    type: Literal["create_rule"] = "create_rule"
    workspace_path: str
    name: str = Field(min_length=1)


class GetCharter(ClientMessage):
    """Load a workspace's signed charter (TD-4002). Human path."""

    type: Literal["get_charter"] = "get_charter"
    workspace_path: str


class SaveCharter(ClientMessage):
    """Write the charter as the human (TD-4002). Never a tool.

    ``charter`` is a mapping, not a second schema: the daemon renders
    YAML and runs :func:`tstd.autonomy.charter.parse_charter` so unknown
    keys and missing fields fail with the field name.  Does not commit.
    """

    type: Literal["save_charter"] = "save_charter"
    workspace_path: str
    charter: dict[str, Any]
    notes: str = ""


class StartAutonomy(ClientMessage):
    """Sign the charter and start an autonomous run (TD-4003). Human path.

    Optional ``charter`` is the same loose mapping ``save_charter``
    takes: write, then commit, then refuse unless the charter gate and
    the sandbox are both live. Does not open an interactive session.
    """

    type: Literal["start_autonomy"] = "start_autonomy"
    workspace_path: str
    charter: dict[str, Any] | None = None
    notes: str = ""


class ListPins(ClientMessage):
    """List a workspace's context pins (TD-2804). Human path."""

    type: Literal["list_pins"] = "list_pins"
    workspace_path: str


class AddPin(ClientMessage):
    """Pin a workspace file or folder (TD-2804). Human path."""

    type: Literal["add_pin"] = "add_pin"
    workspace_path: str
    path: str = Field(min_length=1)


class RemovePin(ClientMessage):
    """Unpin a path without deleting the file (TD-2804)."""

    type: Literal["remove_pin"] = "remove_pin"
    workspace_path: str
    path: str = Field(min_length=1)


class MemoryAccept(ClientMessage):
    """Accept a distill proposal as-is (TD-2401). Write is TD-2402."""

    type: Literal["memory_accept"] = "memory_accept"
    session_id: str
    proposal_id: str


class MemoryFileEdit(BaseModel):
    """One edited file in a ``memory_edit`` (empty content is a delete)."""

    path: str = Field(min_length=1)
    content: str


class MemoryEdit(ClientMessage):
    """Accept a distill proposal with edited bytes (TD-2401 / TD-2403)."""

    type: Literal["memory_edit"] = "memory_edit"
    session_id: str
    proposal_id: str
    files: list[MemoryFileEdit]


class MemoryReject(ClientMessage):
    """Reject a distill proposal. Writes nothing (TD-2401)."""

    type: Literal["memory_reject"] = "memory_reject"
    session_id: str
    proposal_id: str


class EndSession(ClientMessage):
    """Run distill for one session (TD-2302). Not a kill."""

    type: Literal["end_session"] = "end_session"
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


class ArchiveSession(ClientMessage):
    """File a session away, or restore it (TD-1715).

    Archiving is a filing action, never a kill: an archived session keeps
    its loop, its event log, and any turn already in flight.  The daemon
    answers with a refreshed ``session_list``.
    """

    type: Literal["archive_session"] = "archive_session"
    session_id: str
    archived: bool = True


class SetSessionStar(ClientMessage):
    """Star or unstar a session on this machine (TD-3003).

    Stars live in the user data dir, not the workspace. Allowed mid-turn:
    metadata only. The daemon answers with a refreshed ``session_list``.
    """

    type: Literal["set_session_star"] = "set_session_star"
    session_id: str
    starred: bool = True


class DeleteSession(ClientMessage):
    """Destroy a session and its event log (TD-1715).

    Irreversible, so the client confirms first.  Refused while a turn is in
    flight — the answer the user is waiting on would vanish mid-sentence.
    The append-only audit log is untouched: it is the forensic record
    (§2.2), not the session's history.  Answered with ``session_list``.
    """

    type: Literal["delete_session"] = "delete_session"
    session_id: str


class MoveSession(ClientMessage):
    """Reassign a session to another workspace (TD-1715 move to project).

    The session, its id, and its event log all survive — only the working
    context moves, so the agent's cwd and boundary root change on the next
    turn.  The daemon validates the target and re-resolves the boundary and
    policy from it.  Refused while a turn is in flight, because the running
    turn is already executing against the old root.  Answered with
    ``session_list``.
    """

    type: Literal["move_session"] = "move_session"
    session_id: str
    workspace_path: str = Field(min_length=1)


class RenameSession(ClientMessage):
    """Rename a session, or restore its auto-title (TD-3002).

    Metadata only: allowed while a turn is in flight. An empty or
    whitespace-only title restores the first-message auto-title (or
    leaves the row untitled so the rail falls back to the short id).
    The daemon answers with a refreshed ``session_list``.
    """

    type: Literal["rename_session"] = "rename_session"
    session_id: str
    title: str


class GetSetupState(ClientMessage):
    """Request the onboarding setup state (TD-1101 first-run wizard).

    The daemon answers with a ``setup_state`` event: whether an API key is
    stored, the declared presets, and the active one.
    """

    type: Literal["get_setup_state"] = "get_setup_state"


class SetApiKey(ClientMessage):
    """Store an API key in the OS keychain (TD-1101, TD-1717).

    The key never appears in any event, log, or audit record — the ack is a
    refreshed ``setup_state`` event whose ``has_api_key`` flips true.
    ``credential`` is the catalog id (default ``openrouter``). ``name``
    creates or updates the display name; omitted keeps the existing name
    or titles the id.
    """

    type: Literal["set_api_key"] = "set_api_key"
    api_key: str = Field(min_length=1)
    credential: str | None = None
    name: str | None = None


class ValidateApiKey(ClientMessage):
    """Probe a key with one cheap live call (TD-1101, TD-1106, TD-1717).

    With ``api_key`` set, the key currently typed in the wizard is checked
    directly — validation never depends on keychain state.  Without it,
    the stored key is probed.  ``credential`` selects which stored key
    when more than one exists.  The daemon answers with
    ``api_key_validated``; the key itself never appears in any event, log,
    or audit record.
    """

    type: Literal["validate_api_key"] = "validate_api_key"
    api_key: str | None = None
    credential: str | None = None


class DeleteApiKey(ClientMessage):
    """Remove an API key from the OS keychain (TD-1102, TD-1717).

    ``provider`` is the credential id (kept for the existing wire). The
    catalog row stays so the name can be re-keyed. The daemon answers
    with a refreshed ``setup_state``, same ack pattern as ``set_api_key``.
    """

    type: Literal["delete_api_key"] = "delete_api_key"
    provider: str = "openrouter"


class SetPreset(ClientMessage):
    """Pick the active model preset (TD-1101); applied to new sessions."""

    type: Literal["set_preset"] = "set_preset"
    name: str = Field(min_length=1)


class SetTierSlug(ClientMessage):
    """Name the model one tier of one preset uses (TD-1703).

    Deliberately narrow rather than a general ``update_config``: §2.2 forbids
    a secret reaching a config file, and a message that carries only a tier
    name and a slug cannot smuggle one in by construction.  Widening this
    later is easy; narrowing a shipped message is not.

    Persisted to the user's ``config.yaml`` — never the packaged copy, which
    an upgrade overwrites — and acked with ``setup_state``, same as
    ``set_preset``.  Applied to new sessions.
    """

    type: Literal["set_tier_slug"] = "set_tier_slug"
    preset: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    slug: str = Field(min_length=1)


class SetCredential(ClientMessage):
    """Create or rename a named API key without touching the secret (TD-1717).

    ``credential`` omitted slugifies ``name`` into a new id. Present, it
    renames that catalog row. Acked with ``setup_state``.
    """

    type: Literal["set_credential"] = "set_credential"
    name: str = Field(min_length=1, max_length=40)
    credential: str | None = None


class DeleteCredential(ClientMessage):
    """Remove a named key: catalog row, secret, and tier bindings (TD-1717)."""

    type: Literal["delete_credential"] = "delete_credential"
    credential: str = Field(min_length=1)


class SetTierCredential(ClientMessage):
    """Bind a named key to one tier of one preset (TD-1717).

    Empty ``credential`` unbinds. Narrow like ``set_tier_slug``: this
    cannot smuggle a secret into ``config.yaml``. Acked with ``setup_state``.
    """

    type: Literal["set_tier_credential"] = "set_tier_credential"
    preset: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    credential: str = ""


class SetMcpServer(ClientMessage):
    """Create or replace one listed MCP server (TD-4403).

    Command is argv tokens only. There is no ``env`` field — a token
    pasted here would land in ``config.yaml``. Extra keys are forbidden
    so ``env`` / ``environment`` cannot sneak in later. Acked with
    ``setup_state``. Live sessions keep their previous tool set until a
    new session; hot-attach is out of scope.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_mcp_server"] = "set_mcp_server"
    id: str = Field(min_length=1)
    transport: Literal["stdio", "http"]
    command: list[str] = Field(default_factory=list)
    url: str = ""
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _refuse_env(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("env", "environment"):
                if key in value:
                    raise ValueError(
                        "MCP server messages cannot carry env; tokens stay in the keychain"
                    )
        return value

    @field_validator("command", mode="before")
    @classmethod
    def _command_is_argv(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raise ValueError("command must be a list of argv tokens, not a string")
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("command must be a list of argv tokens")


class DeleteMcpServer(ClientMessage):
    """Remove one listed MCP server (TD-4403). Unknown id is a typed error."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["delete_mcp_server"] = "delete_mcp_server"
    id: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _refuse_env(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("env", "environment"):
                if key in value:
                    raise ValueError(
                        "MCP server messages cannot carry env; tokens stay in the keychain"
                    )
        return value


class RunDiagnostics(ClientMessage):
    """Ask the daemon to run the doctor checks (TD-1104 diagnostics).

    The daemon answers with a single ``diagnostics_report`` once every
    check completes — including the one-token provider probe, so the
    reply can take a second or two.
    """

    type: Literal["run_diagnostics"] = "run_diagnostics"


class GetUsage(ClientMessage):
    """Ask for the audit store's usage rollups (TD-1706).

    The daemon answers with one ``usage_report`` carrying every bucket —
    session, day, and week — split by tier.  Connection-scoped, like
    ``get_setup_state``: the audit database spans every session, so
    scoping the question to one of them would answer a different one.
    """

    type: Literal["get_usage"] = "get_usage"


class ExportUsage(ClientMessage):
    """Write the audit store's model-call records to a file (TD-1706).

    Carries the format and nothing else.  The destination is the daemon's
    own exports directory, deliberately not a client-supplied path: this
    message would otherwise be a general "write a file anywhere" verb
    reachable from the socket, which is a wider hole than the feature
    needs.  The chosen path comes back on ``usage_exported``.
    """

    type: Literal["export_usage"] = "export_usage"
    format: Literal["jsonl", "csv"] = "jsonl"


class ListArtifacts(ClientMessage):
    """List artifacts persisted with a session (TD-3201).

    The daemon answers with ``artifact_list``.  Bytes stay off the wire.
    """

    type: Literal["list_artifacts"] = "list_artifacts"
    session_id: str


class OpenArtifact(ClientMessage):
    """Open one artifact by id (TD-3201).

    The daemon answers with ``artifact`` (metadata and path).  An unknown
    id is a typed error.  Huge bytes are not dumped on the socket.
    """

    type: Literal["open_artifact"] = "open_artifact"
    session_id: str
    artifact_id: str = Field(min_length=1)


class DesignHitTest(ClientMessage):
    """Ask the session browser what is at a CSS-pixel point (TD-3403).

    Observe only — never actuates. The reply is connection-scoped
    ``design_hit``, not a session-log event.
    """

    type: Literal["design_hit_test"] = "design_hit_test"
    session_id: str
    x: float
    y: float


class CheckCuPermissions(ClientMessage):
    """Re-probe computer-use OS permissions / integrity (TD-3302, TD-3303).

    Connection-scoped.  The daemon answers with ``cu_permissions``.  The
    probe never raises a TCC prompt and never waits on a Windows grant
    dialog (there is none) — a hang is a defect.
    """

    type: Literal["check_cu_permissions"] = "check_cu_permissions"


class SetCuKill(ClientMessage):
    """Engage or clear the process-wide computer-use kill-switch (TD-3404).

    Connection-scoped: the flag is shared across every session on this
    daemon, so the message carries no ``session_id``. Capture
    (screenshot) still runs. Acked with ``cu_kill_state``.
    """

    type: Literal["set_cu_kill"] = "set_cu_kill"
    killed: bool


class ListJobs(ClientMessage):
    """List persisted scheduled jobs (TD-3805).

    Connection-scoped: jobs live in the user data dir, not a session.
    The daemon answers with ``job_list``. Does not run anything.
    """

    type: Literal["list_jobs"] = "list_jobs"


class SaveJob(ClientMessage):
    """Create or replace a scheduled job (TD-3805).

    Draft fields, not a natural-language parse. Pause is this verb with
    ``paused`` set. The runner (TD-3804) is the only code that fires
    jobs. Acked with ``job_list``.
    """

    type: Literal["save_job"] = "save_job"
    id: str | None = None
    workspace: str | None = None
    instruction: str | None = None
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy"] | None = None
    paused: bool = False


class DeleteJob(ClientMessage):
    """Remove a scheduled job by id (TD-3805).

    Acked with ``job_list``. Unknown id is a typed error. Does not run
    anything.
    """

    type: Literal["delete_job"] = "delete_job"
    job_id: str = Field(min_length=1)


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


class ConversationReset(DaemonEvent):
    """The conversation forked or a sibling was selected (TD-1708).

    The viewer drops every row after the ``user_index``-th user message,
    replaces that user message with ``content``, and treats later events
    as the active sibling.  ``seq`` is stamped by the session log.
    """

    type: Literal["conversation_reset"] = "conversation_reset"
    session_id: str
    user_index: int
    sibling_index: int
    sibling_count: int
    content: str


class UserTurn(DaemonEvent):
    """A user message the loop has accepted (needed for honest replay).

    The client echoes locally on send. This event exists so a restarted
    daemon can replay the user's side of the transcript without inventing
    it. ``turn_id`` lets a live echo and the replayed event be the same
    row rather than a duplicate.
    """

    type: Literal["user_turn"] = "user_turn"
    session_id: str
    turn_id: str
    content: str


class AssistantDelta(DaemonEvent):
    """A streamed chunk of assistant output."""

    type: Literal["assistant_delta"] = "assistant_delta"
    session_id: str
    delta: str


class AssistantReasoning(DaemonEvent):
    """A streamed chunk of a reasoning model's thinking (TD-1901).

    Deliberately not an ``assistant_delta`` with a flag.  The transcript
    has to be able to tell thinking from answer long after the stream
    ended — to fold one and not the other, and to keep reasoning out of
    what is replayed to the provider — and a flag on a shared type makes
    that a runtime check every consumer has to remember to perform.
    """

    type: Literal["assistant_reasoning"] = "assistant_reasoning"
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


class VerifyResult(DaemonEvent):
    """Interactive validator review of a write turn (TD-4204).

    Timeline activity, not a chat bubble. ``pending`` is ask-mode only:
    the validator has not run until the human confirms.
    """

    type: Literal["verify_result"] = "verify_result"
    session_id: str
    verdict: Literal["pass", "fail", "error"]
    summary: str
    cost: float = Field(ge=0)
    pending: bool = False


class CostUpdate(DaemonEvent):
    """Accrued cost for the session."""

    type: Literal["cost_update"] = "cost_update"
    session_id: str
    # Spend on the current turn so far, across every provider call it has
    # made — a turn using a tool emits several of these, each carrying the
    # running total, and the last agrees with turn_complete.cost (TD-1806).
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
    """Active model tier and configured slugs (TD-1006, TD-1720).

    Emitted when a session opens, when a ``set_tier`` override lands, and
    whenever the router changes tier between turns (lead-turns handoff,
    failure escalation). ``tier`` is what handles the next turn;
    ``override`` is the pinned override when the user picked one.
    ``preset`` and ``hosts`` are the identity the title bar paints;
    ``hosts`` is hostname:port from the URL the client will call, not
    a guess from the slug (TD-1718). ``plan`` is the brain lock
    (TD-4603). Additive — no PROTOCOL_VERSION bump.
    """

    type: Literal["tier_state"] = "tier_state"
    session_id: str
    tier: TierName
    override: TierName | None = None
    # tier name → configured model slug, "brain"/"worker"/"validator".
    model_slugs: dict[str, str] = Field(default_factory=dict)
    preset: str = ""
    # tier name → hostname:port the provider client will call.
    hosts: dict[str, str] = Field(default_factory=dict)
    # Plan mode (TD-4603): when true, every completion is brain until
    # cleared. Default false so older fixtures and emitters stay valid.
    plan: bool = False


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
    # Attachment caps (TD-1709), so the composer refuses oversize files
    # against this workspace's real numbers instead of a hardcoded guess.
    # Additive with a default: a client that ignores it behaves as it did.
    attachments: AttachmentLimits = Field(default_factory=AttachmentLimits)


class TurnComplete(DaemonEvent):
    """Summary of a completed turn.

    ``tokens``, ``cost`` and ``duration`` cover the whole turn — every
    provider call it made, tool round-trips included — not just the call
    that produced the final response (TD-1806).

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
    """Emitted when steering files are re-resolved after a detected change.

    Two token figures, because they answer different questions and one
    cannot stand in for the other: ``prefix_tokens`` is the size of the
    whole cache prefix (base prompt + workspace root + steering), which is
    what the provider re-bills when the hash moves, while
    ``steering_tokens`` is the size of the steering block alone — what the
    user's own files cost.  Reporting only the prefix figure under a
    "steering reloaded" heading overstates steering by the fixed cost of
    the machinery around it (TD-1810 block [1b] and the base prompt).
    """

    type: Literal["steering_reloaded"] = "steering_reloaded"
    session_id: str
    prefix_hash: str
    prefix_tokens: int = Field(ge=0)
    steering_tokens: int = Field(ge=0)
    source_count: int = Field(ge=0)


class RuleActivated(DaemonEvent):
    """A path-scoped rule entered the prompt because the session touched a
    matching file (TD-503).

    Emitted once per rule per session, at the first assembly where the
    rule's ``appliesTo`` globs match a touched path.  The timeline shows
    the injection so context changes are never silent.
    """

    type: Literal["rule_activated"] = "rule_activated"
    session_id: str
    rule_path: str  # workspace-relative path of the rule file


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


class InstructionFileEntry(BaseModel):
    """One file on the project home's Instructions column (TD-2802)."""

    path: str
    name: str
    kind: Literal["agents", "claude", "rule"]


class MemoryFileDiff(BaseModel):
    """One file in a ``memory_proposal`` (TD-2401)."""

    action: Literal["create", "replace", "delete"]
    path: str
    diff: str
    before: str | None = None
    after: str | None = None


class MemoryProposal(DaemonEvent):
    """Distill produced a diff the user must accept, edit, or reject."""

    type: Literal["memory_proposal"] = "memory_proposal"
    session_id: str
    proposal_id: str
    files: list[MemoryFileDiff]


class MemoryFileEntry(BaseModel):
    """One ``.tst/memory/`` file the Memory pane lists (TD-2601)."""

    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: str


class MemoryFiles(DaemonEvent):
    """Reply to ``list_memory``. Connection-scoped."""

    type: Literal["memory_files"] = "memory_files"
    seq: int = 1
    workspace_path: str
    files: list[MemoryFileEntry] = Field(default_factory=list)


class CharterDocument(DaemonEvent):
    """Reply to ``get_charter`` / ``save_charter``. Connection-scoped.

    ``charter`` reuses :class:`tstd.autonomy.charter.Charter` — one
    schema.  Absent file: ``present`` is false and ``charter`` is null.
    """

    type: Literal["charter"] = "charter"
    seq: int = 1
    workspace_path: str
    present: bool
    charter: Charter | None = None
    notes: str = ""


class AutonomyStart(DaemonEvent):
    """Reply to ``start_autonomy``. Connection-scoped.

    ``ready`` is true only when the charter is signed and the sandbox
    is live. When ready, ``session_id`` is the daemon-owned unattended
    run (TD-4101). Interactive sessions are not this event. Seq stays
    connection-scoped (fixed at 1); ``session_id`` is the run to attach.
    """

    type: Literal["autonomy_start"] = "autonomy_start"
    seq: int = 1
    workspace_path: str
    ready: bool
    signed: bool
    error: str | None = None
    session_id: str | None = None


class AutonomySummary(DaemonEvent):
    """Wake-up card for an unattended run that stopped (TD-4303).

    Session-scoped: persisted on the event log so a closed window still
    sees it on attach. Interactive sessions never emit this type.
    """

    type: Literal["autonomy_summary"] = "autonomy_summary"
    session_id: str
    reason: str
    branch: str
    ledger_path: str
    changed: list[str] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)
    ledger_excerpt: str = ""


class ContextPinEntry(BaseModel):
    """One pinned path on the Context column (TD-2804)."""

    path: str
    name: str
    kind: Literal["file", "dir"]
    lines: int = Field(ge=0)


class ContextPins(DaemonEvent):
    """Reply to ``list_pins`` / ``add_pin`` / ``remove_pin``."""

    type: Literal["context_pins"] = "context_pins"
    seq: int = 1
    workspace_path: str
    pins: list[ContextPinEntry] = Field(default_factory=list)
    instruction_tokens: int = Field(default=0, ge=0)
    memory_tokens: int = Field(default=0, ge=0)
    pin_tokens: int = Field(default=0, ge=0)
    capacity_cap: int = Field(default=0, ge=0)
    dropped: list[str] = Field(default_factory=list)


class InstructionFiles(DaemonEvent):
    """Reply to ``list_instructions`` / ``create_rule``. Connection-scoped."""

    type: Literal["instruction_files"] = "instruction_files"
    seq: int = 1
    workspace_path: str
    files: list[InstructionFileEntry] = Field(default_factory=list)
    # Path just created by create_rule, when this is that reply.
    created: str | None = None


class CommandEntry(BaseModel):
    """One slash command the composer can insert (TD-4501)."""

    name: str
    description: str = ""
    source: Literal["workspace", "user", "claude_workspace", "claude_user"]
    body: str = ""
    too_large: bool = False


class CommandList(DaemonEvent):
    """Reply to ``list_commands``. Connection-scoped."""

    type: Literal["command_list"] = "command_list"
    seq: int = 1
    workspace_path: str
    commands: list[CommandEntry] = Field(default_factory=list)


class MemoryStackEntry(BaseModel):
    """One memory file the inspector names (TD-2604)."""

    path: str
    tokens: int = Field(ge=0)
    reason: Literal["always-index", "heading", "embedding"]


class SkillStackEntry(BaseModel):
    """One discovered skill on the instruction stack (TD-4502)."""

    name: str
    description: str = ""
    source: Literal["workspace", "user", "claude_workspace", "claude_user"]
    loaded: bool = False
    tokens: int = Field(ge=0)


class InstructionStack(DaemonEvent):
    """Response to ``get_instruction_stack``: the resolved stack with counts."""

    type: Literal["instruction_stack"] = "instruction_stack"
    session_id: str
    sources: list[InstructionStackEntry] = Field(default_factory=list)
    total_tokens: int = Field(ge=0)
    token_method: str
    # Cached prompt tokens the provider reported on the most recent
    # main-loop call (TD-1201's "is the block currently cached"). ``None``
    # means no figure to report — either no turn has run yet, or the
    # provider sent none. Never 0 on a missing field: an engine that says
    # nothing about reuse has not reported a miss (TD-1811).
    last_cached_tokens: int | None = None
    # Whether a main-loop call has come back at all. Tells the two ``None``
    # cases apart, so the viewer can say "no turn yet" and "this provider
    # does not report cache reuse" instead of guessing between them
    # (TD-1811). Additive with a safe default — no PROTOCOL_VERSION bump.
    cache_observed: bool = False
    # Last brain-turn memory selection (TD-2604). Empty + placeholder
    # means none loaded — the prompt still carries MEMORY_PLACEHOLDER.
    memory: list[MemoryStackEntry] = Field(default_factory=list)
    memory_dropped: list[MemoryStackEntry] = Field(default_factory=list)
    memory_placeholder: bool = False
    # Discovered skills, separate from steering sources (TD-4502).
    skills: list[SkillStackEntry] = Field(default_factory=list)


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
    # Filed away by the user (TD-1715). The list stays complete — one list
    # keeps the rail, the recents menu, and the chat pane's auto-bind
    # reading the same event — and this flag is what "hidden from the
    # default list" is rendered from. Additive with a default, so a client
    # that ignores it behaves exactly as it did.
    archived: bool = False
    # Pinned to the top of the rail (TD-3003). Machine-wide; not the
    # workspace. Additive with a default, so a client that ignores it
    # still sorts newest-first.
    starred: bool = False
    # Auto-title from the first non-empty user message (TD-3001). None
    # until then — the rail falls back to the short id. Additive.
    title: str | None = None
    # Catalog preset this session opened with, or last switched to
    # (TD-1721). Empty on a store written before the field. Additive.
    preset: str = ""


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


class CredentialSummary(BaseModel):
    """One named API key as the UI may see it (TD-1717). Never the secret.

    ``base_url`` is the host this key talks to (TD-1718). Additive; an
    older client ignores it. ``None`` means the tier URL stays in charge.
    """

    id: str
    name: str
    stored: bool
    base_url: str | None = None


class McpServerSummary(BaseModel):
    """One listed MCP server as shown in settings (TD-4403). Never a secret."""

    id: str
    transport: Literal["stdio", "http"]
    command: list[str] = Field(default_factory=list)
    url: str = ""
    enabled: bool = True


class SetupState(DaemonEvent):
    """Response to ``get_setup_state``; also the ack for ``set_api_key`` and
    ``set_preset`` (TD-1101).

    Connection-scoped (like ``session_list``), so its seq is fixed at 1.
    ``has_api_key`` is the first-run signal: no key stored means the wizard
    shows — unless ``key_required`` is False, which says the active preset
    runs entirely on loopback endpoints and will never send a key (TD-1801).
    Defaulting to True keeps a client that ignores the field behaving as it
    did before, so this is an additive field and not a version bump.
    """

    type: Literal["setup_state"] = "setup_state"
    seq: int = 1
    has_api_key: bool
    key_required: bool = True
    presets: list[str] = Field(default_factory=list)
    active_preset: str
    # The active preset's slug per tier, for the settings screen's model
    # section (TD-1703). ``None`` where a loopback tier leaves its model to
    # discovery (TD-1805) — the UI shows that as discovered, not as blank.
    # Additive with a default, like ``key_required``: an older client that
    # ignores it behaves exactly as it did.
    tier_slugs: dict[str, str | None] = Field(default_factory=dict)
    # TD-804: skip-all approvals. Additive, default off — an older client
    # that ignores the field keeps asking, which is the safe read.
    skip_all_approvals: bool = False
    # TD-2603: load ``~/.tstdesk/memory/`` after workspace memory.
    # Additive, default off — off means that directory is never read.
    load_global_memory: bool = False
    # TD-2905: keep running when the window closes. Additive, default on —
    # a missing file and an older client that ignores the field stay on.
    coworker_enabled: bool = True
    # TD-3402: Screen-pane glow / agent cursor. Additive, default on.
    cu_glow: bool = True
    cu_agent_cursor: bool = True
    # TD-3402: sidecar glow on the real display. Additive, default on now
    # that the ring exists. Hidden for the duration of every screenshot.
    cu_show_on_real_display: bool = True
    # TD-2806: workspaces pinned on this machine. Not a workspace file.
    pinned_workspaces: list[str] = Field(default_factory=list)
    # TD-3603: Settings "Allow remote attach". Additive, default off.
    # ``remote_bind`` is the bound Tailscale address, never a token.
    remote_attach_enabled: bool = False
    remote_bind: str | None = None
    # TD-1717: named keys. Additive; an older client ignores them.
    # ``stored`` is a keychain probe, never the secret. ``tier_credentials``
    # is the configured binding (None = unbound). ``tier_loopback`` lets
    # the model picker offer "no key" only on loopback tiers.
    # ``credentials[].base_url`` is the key's host (TD-1718).
    credentials: list[CredentialSummary] = Field(default_factory=list)
    tier_credentials: dict[str, str | None] = Field(default_factory=dict)
    tier_loopback: dict[str, bool] = Field(default_factory=dict)
    # TD-4403: listed MCP servers. Additive, default empty. Never a secret;
    # there is no env map. An older client ignores the field.
    mcp_servers: list[McpServerSummary] = Field(default_factory=list)


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


class UsageRollup(BaseModel):
    """One bucket's spend on one tier (TD-1706).

    ``key`` is a session id, an ISO day, or the ISO day the week opened
    on, depending on ``bucket``.  Classifier spend (TD-703) stays on its
    own field here exactly as it does in the store — folding it into
    ``cost`` would make the view disagree with the title-bar meter.
    """

    bucket: Literal["session", "day", "week"]
    key: str
    tier: str
    prompt_tokens: int = Field(ge=0)
    cached_prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost: float = Field(ge=0)
    classifier_cost: float = Field(default=0.0, ge=0)


class UsageReport(DaemonEvent):
    """Response to ``get_usage`` (TD-1706): every bucket, split by tier.

    Connection-scoped like ``diagnostics_report``, so its seq is fixed at
    1 — the audit database is a fact about the machine, not about any one
    session's event log.  All three buckets ride one message because the
    view switches between them locally and a round trip per tab would
    show stale numbers next to fresh ones.
    """

    type: Literal["usage_report"] = "usage_report"
    seq: int = 1
    rows: list[UsageRollup] = Field(default_factory=list)


class UsageExported(DaemonEvent):
    """Response to ``export_usage`` (TD-1706): where the file landed.

    ``rows`` is the model-call record count written, so the client can
    say "42 calls" rather than claim success over an empty file.
    """

    type: Literal["usage_exported"] = "usage_exported"
    seq: int = 1
    format: Literal["jsonl", "csv"]
    path: str
    rows: int = Field(ge=0)


class LogTrimmed(DaemonEvent):
    """Attach asked for a seq the on-disk window has dropped (TD-2901).

    Connection-scoped: sent on the attach socket only, never written to
    the session log, so it does not consume a seq. ``seq`` is fixed at 1
    like ``usage_report``. The client jumps its cursor to
    ``earliest_seq - 1`` so replay from the kept window is not a false gap.
    """

    type: Literal["log_trimmed"] = "log_trimmed"
    seq: int = 1
    session_id: str
    requested_from_seq: int = Field(ge=1)
    earliest_seq: int = Field(ge=1)


class ArtifactEntry(BaseModel):
    """One artifact on ``artifact_list`` (TD-3201). Path, not bytes."""

    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str


class ArtifactReady(DaemonEvent):
    """An artifact was recorded for this session (TD-3201).

    Session-scoped: it rides the event log so attach can replay the
    notice.  ``list_artifacts`` is the source of truth after a trim.
    """

    type: Literal["artifact_ready"] = "artifact_ready"
    session_id: str
    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str


class ArtifactList(DaemonEvent):
    """Response to ``list_artifacts`` (TD-3201). Connection-scoped."""

    type: Literal["artifact_list"] = "artifact_list"
    seq: int = 1
    session_id: str
    artifacts: list[ArtifactEntry] = Field(default_factory=list)


class Artifact(DaemonEvent):
    """Response to ``open_artifact`` (TD-3201): metadata and path.

    Connection-scoped.  Bytes stay on disk; the client opens the path.
    """

    type: Literal["artifact"] = "artifact"
    seq: int = 1
    session_id: str
    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str


class Ping(BaseModel):
    """Application-level liveness frame (TD-1716).  No session, no seq.

    Deliberately not a ``DaemonEvent``: ``seq`` is the per-session event
    log's contract and a ping belongs to no session's log — it is emitted
    on a timer, replays nothing, and must never advance a client's
    sequence bookkeeping.  It is nonetheless a daemon→client frame, so it
    parses through ``parse_daemon_event`` like every other one.

    Transport ping/pong cannot do this job: the OS network stack answers
    those while a suspended webview's JavaScript is frozen, so a pong
    proves the machine is alive, not the client.  Only a frame the
    client's own event loop must process proves that.
    """

    type: Literal["ping"] = "ping"


class Error(DaemonEvent):
    """A typed error, usually in response to a bad message."""

    type: Literal["error"] = "error"
    session_id: str | None = None
    code: str
    message: str


class ScreenFrame(DaemonEvent):
    """A computer-use screenshot written to the session dir (TD-1710, TD-3401).

    Path, not bytes. Browser and desktop share this event. The PNG lives
    under ``sessions/<id>/`` — the same wall as artifacts. A text
    data-URL sidecar lets the Screen pane preview through the existing
    host reader.
    """

    type: Literal["screen_frame"] = "screen_frame"
    session_id: str
    path: str
    mime: str = "image/png"
    width: int | None = None
    height: int | None = None
    tool_call_id: str | None = None


class CuKillState(DaemonEvent):
    """Current computer-use kill-switch (TD-3404).

    Connection-scoped with ``seq`` fixed at 1: the flag is process-wide,
    not a fact in any session's event log. Replaying it from a session
    log would desync every other viewer. Capture still runs when
    ``killed`` is true. ``killed=true`` also clears Screen-pane glow
    and cursor (TD-3402).
    """

    type: Literal["cu_kill_state"] = "cu_kill_state"
    seq: int = 1
    killed: bool


class CuSession(DaemonEvent):
    """Computer-use episode open/close (TD-3407).

    ``active=true`` on the first ``desktop_*`` / ``browser_*`` tool of a
    turn. ``active=false`` on turn end, cancel, or kill-switch. The
    real-display ring and Screen-pane glow follow this tag, not an
    actuation linger.
    """

    type: Literal["cu_session"] = "cu_session"
    session_id: str
    active: bool


class DesignHitBox(BaseModel):
    """Computed box of a Design-mode hit, in frame CSS pixels."""

    x: float
    y: float
    width: float
    height: float


class DesignHit(DaemonEvent):
    """Reply to ``design_hit_test`` (TD-3403). Connection-scoped.

    Not written to the session log — a pick is user inspection, not an
    agent turn. ``seq`` is fixed at 1 so it cannot rewind attach.
    """

    type: Literal["design_hit"] = "design_hit"
    seq: int = 1
    session_id: str
    x: float
    y: float
    xpath: str | None = None
    role: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    box: DesignHitBox | None = None
    styles: dict[str, str] = Field(default_factory=dict)


class CuPermissions(DaemonEvent):
    """Computer-use OS permission / integrity report (TD-3302, TD-3303).

    Connection-scoped (seq is fixed at 1).  ``first_run`` is true when
    this is the first desktop CU attempt for this platform on this
    machine.

    macOS carries granted/denied for Screen Recording and Accessibility
    plus System Settings deep links.  Windows has no grant dialog — the
    same event names the two silent failure modes (UIPI, secure desktop)
    with empty settings URLs.  Linux X11 is the same kind of honesty
    (no TCC analog); Wayland and missing XTEST are named limits, not a
    grant dialog (TD-2001).
    """

    type: Literal["cu_permissions"] = "cu_permissions"
    seq: int = 1
    granted: bool
    screen_recording: bool
    accessibility: bool
    screen_recording_url: str
    accessibility_url: str
    first_run: bool = False
    platform: Literal["macos", "windows", "linux"] = "macos"
    no_gate: str = ""
    uipi: str = ""
    secure_desktop: str = ""
    elevated: bool = False
    uipi_applies: bool = False
    secure_desktop_applies: bool = False
    session_type: str = ""
    wayland: str = ""
    wayland_applies: bool = False
    xtest: str = ""
    xtest_applies: bool = False
    no_display: str = ""
    no_display_applies: bool = False


class JobEntry(BaseModel):
    """One persisted job on ``job_list`` (TD-3805)."""

    id: str
    workspace: str
    instruction: str
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy"]
    paused: bool = False


class JobList(DaemonEvent):
    """Response to ``list_jobs`` / ``save_job`` / ``delete_job`` (TD-3805).

    Connection-scoped. Seq is fixed at 1 so it cannot rewind attach.
    """

    type: Literal["job_list"] = "job_list"
    seq: int = 1
    jobs: list[JobEntry] = Field(default_factory=list)


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
    | ForkFrom
    | SetBranch
    | SetSkipAllApprovals
    | SetLoadGlobalMemory
    | SetCoworker
    | SetCuIndicators
    | SetWorkspacePin
    | Resume
    | Cancel
    | RunVerify
    | DenyVerify
    | Attach
    | Detach
    | SetTier
    | SetPlan
    | SetSessionPreset
    | GetInstructionStack
    | ListInstructions
    | ListCommands
    | ListMemory
    | SaveMemory
    | CreateRule
    | GetCharter
    | SaveCharter
    | StartAutonomy
    | ListPins
    | AddPin
    | RemovePin
    | MemoryAccept
    | MemoryEdit
    | MemoryReject
    | EndSession
    | Shutdown
    | ListSessions
    | NewSession
    | ArchiveSession
    | SetSessionStar
    | DeleteSession
    | MoveSession
    | RenameSession
    | GetSetupState
    | SetApiKey
    | ValidateApiKey
    | SetPreset
    | SetTierSlug
    | SetCredential
    | DeleteCredential
    | SetTierCredential
    | SetMcpServer
    | DeleteMcpServer
    | RunDiagnostics
    | GetUsage
    | ExportUsage
    | DeleteApiKey
    | ListArtifacts
    | OpenArtifact
    | DesignHitTest
    | CheckCuPermissions
    | SetCuKill
    | SetRemoteAttach
    | ListJobs
    | SaveJob
    | DeleteJob,
    Field(discriminator="type"),
]

DaemonEventT = Annotated[
    Ready
    | SessionState
    | ConversationReset
    | UserTurn
    | AssistantDelta
    | AssistantReasoning
    | ToolCall
    | ToolResult
    | ShellOutput
    | ApprovalRequest
    | DecisionLogged
    | CheckpointNotice
    | VerifyResult
    | CostUpdate
    | BoundaryUpdate
    | TurnComplete
    | TierState
    | ContextCompacted
    | SteeringReloaded
    | RuleActivated
    | TierSwitched
    | InstructionStack
    | InstructionFiles
    | CommandList
    | ContextPins
    | MemoryFiles
    | CharterDocument
    | AutonomyStart
    | AutonomySummary
    | MemoryProposal
    | SessionList
    | PolicyRules
    | SetupState
    | ApiKeyValidated
    | DiagnosticsReport
    | UsageReport
    | UsageExported
    | LogTrimmed
    | ArtifactReady
    | ArtifactList
    | Artifact
    | Ping
    | Error
    | ScreenFrame
    | CuKillState
    | CuSession
    | DesignHit
    | CuPermissions
    | JobList,
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
        "fork_from",
        "set_branch",
        "set_skip_all_approvals",
        "set_load_global_memory",
        "set_coworker",
        "set_cu_indicators",
        "set_workspace_pin",
        "resume",
        "cancel",
        "run_verify",
        "deny_verify",
        "attach",
        "detach",
        "set_tier",
        "set_plan",
        "set_session_preset",
        "get_instruction_stack",
        "list_instructions",
        "list_commands",
        "list_memory",
        "save_memory",
        "create_rule",
        "get_charter",
        "save_charter",
        "start_autonomy",
        "list_pins",
        "add_pin",
        "remove_pin",
        "memory_accept",
        "memory_edit",
        "memory_reject",
        "end_session",
        "shutdown",
        "list_sessions",
        "new_session",
        "archive_session",
        "set_session_star",
        "delete_session",
        "move_session",
        "rename_session",
        "get_setup_state",
        "set_api_key",
        "validate_api_key",
        "delete_api_key",
        "set_preset",
        "set_tier_slug",
        "set_credential",
        "delete_credential",
        "set_tier_credential",
        "set_mcp_server",
        "delete_mcp_server",
        "run_diagnostics",
        "get_usage",
        "export_usage",
        "list_artifacts",
        "open_artifact",
        "design_hit_test",
        "check_cu_permissions",
        "set_cu_kill",
        "set_remote_attach",
        "list_jobs",
        "save_job",
        "delete_job",
    }
)
_KNOWN_EVENT_TYPES = frozenset(
    {
        "ready",
        "session_state",
        "conversation_reset",
        "user_turn",
        "assistant_delta",
        "assistant_reasoning",
        "tool_call",
        "tool_result",
        "shell_output",
        "approval_request",
        "decision_logged",
        "checkpoint_notice",
        "verify_result",
        "cost_update",
        "boundary_update",
        "turn_complete",
        "tier_state",
        "context_compacted",
        "steering_reloaded",
        "rule_activated",
        "tier_switched",
        "instruction_stack",
        "instruction_files",
        "command_list",
        "context_pins",
        "memory_files",
        "charter",
        "autonomy_start",
        "autonomy_summary",
        "memory_proposal",
        "session_list",
        "policy_rules",
        "setup_state",
        "api_key_validated",
        "diagnostics_report",
        "usage_report",
        "usage_exported",
        "log_trimmed",
        "artifact_ready",
        "artifact_list",
        "artifact",
        "ping",
        "error",
        "screen_frame",
        "cu_kill_state",
        "cu_session",
        "design_hit",
        "cu_permissions",
        "job_list",
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


def build_ping() -> str:
    """Build the application-level liveness frame (TD-1716)."""
    return Ping().model_dump_json()


def build_error(code: str, message: str, session_id: str | None = None) -> str:
    """Build a typed error message.

    The message passes through the shared redaction chokepoint (TD-1405):
    this envelope bypasses the event log — it is written straight to the
    socket — so it must scrub here rather than rely on ``event_log.add``.
    ``session_id`` (optional, TD-1711) lets the UI attribute the error to
    the session whose message was refused.
    """
    payload: dict[str, str] = {"type": "error", "code": code, "message": redact_secrets(message)}
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload)


def scrub_wire_json(payload: str) -> str:
    """Redact secret-shaped strings in a serialized daemon→client frame.

    The daemon's reply funnel runs every direct reply through here
    (TD-4802): connection-scoped replies (``session_list``,
    ``memory_files``, ``setup_state``, …) bypass the session event log,
    where redaction normally happens at insertion, and their payloads can
    be user text — a session title is the first user message.  Idempotent:
    a frame already scrubbed at insertion passes through unchanged.
    """
    return json.dumps(redact_structure(json.loads(payload)), ensure_ascii=False)


def validate_hello(hello: Hello) -> None:
    """Validate the version and token of a parsed hello message."""
    validate_version(hello.version)
