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
import re
import secrets
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

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
    """Opening handshake: token + protocol version.

    ``features`` (TD-5015) is what the client opts into. ``"machines"``
    makes a loopback or phone connection a *viewer*: its session lists
    carry other computers' rows and it gets ``machine_list``.
    ``"bridge"`` is what a hub sends when it dials another computer; it
    makes the connection a machine peer. Unknown names are ignored, so
    an old daemon ignores the whole field.
    """

    type: Literal["hello"] = "hello"
    token: str = Field(min_length=1)
    version: int = Field(ge=0, description="Validated by validate_hello()")
    features: list[Annotated[str, Field(max_length=32)]] = Field(default_factory=list, max_length=8)


class Pair(ClientMessage):
    """Redeem a pairing code as the first frame, instead of hello (TD-5014).

    Remote peers only. Success is an out-of-band ``pair_ok`` carrying the
    device's own key, then close 1000; the phone then reconnects with
    ``hello``. ``code`` is normalized by the daemon, never logged.
    ``device_name`` is cleaned on the daemon side (empty is "Unnamed device").
    ``replaces`` is the phone's current device key when it has one: once
    the new key is out, the device that key verifies as is removed and
    its connections closed. A legacy remote-token there retires nothing.
    Never logged.
    """

    type: Literal["pair"] = "pair"
    code: str = Field(min_length=1)
    device_name: str = ""
    version: int = Field(ge=0, description="Validated like hello's version")
    replaces: str | None = None
    # TD-5015: another computer pairing as a hub sends "machine". It grants
    # nothing: a machine-kind device gets fewer verbs than a phone.
    kind: Literal["phone", "machine"] = "phone"


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

    ``steer`` (TD-5112) asks for the message to join the turn that is
    running when it arrives, at that turn's next tool-batch boundary: the
    window's Send now mid-turn sets it. Without it, or with no turn
    running, the message waits for a turn of its own, as before. Additive:
    a client that never sends it never steers.
    """

    type: Literal["user_message"] = "user_message"
    session_id: str
    content: str
    attachments: list[Attachment] = Field(default_factory=list)
    steer: bool = False


class SendQueued(ClientMessage):
    """Send a message a restart left held (TD-5155).

    ``queued_id`` names a row of the session's ``queued_messages``. The
    message joins the loop's queue as if just sent: it starts a turn when
    the loop is free, so a drain hold refuses it like ``user_message``.
    """

    type: Literal["send_queued"] = "send_queued"
    session_id: str
    queued_id: str


class RemoveQueued(ClientMessage):
    """Drop a message a restart left held, unsent (TD-5155)."""

    type: Literal["remove_queued"] = "remove_queued"
    session_id: str
    queued_id: str


class ForkFrom(ClientMessage):
    """Replace a past user turn and fork the conversation from there (TD-1708).

    ``user_index`` is 0-based among user turns.  Refused while a turn is
    in flight.  The daemon answers with ``conversation_reset``.
    """

    type: Literal["fork_from"] = "fork_from"
    session_id: str
    user_index: int
    content: str


class CompactContext(ClientMessage):
    """Compact the conversation now (TD-5109).

    Without ``user_index``: everything before the last user turn becomes
    a summary (``/compact`` and the gauge's button).  With it ("Summarize
    from here"): that user turn and everything after it become a summary,
    and earlier turns stay verbatim.  ``user_index`` counts user turns
    from 0, as ``fork_from`` does.  ``guidance`` heads the summary and
    decides what it keeps.  Refused while a turn is in flight.
    """

    type: Literal["compact_context"] = "compact_context"
    session_id: str
    guidance: str = Field(default="", max_length=2000)
    user_index: int | None = Field(default=None, ge=0)


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
    when the call is a class-C decision, and with ``not_always_allowable``
    when it is a shell command chained from several commands or one policy
    cannot split with confidence (TD-5101): one rule cannot cover it, so
    the approval stays pending.  While the workspace config is not trusted
    (TD-5113) nothing is saved: the call is approved once and the reply is
    ``workspace_config_untrusted``.
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


class SetJudgments(ClientMessage):
    """Judgment-seam feature toggles (TD-708/709/710/711, dev build).

    Machine-wide, no session. Persists the ``judgments:`` block of the
    user config; the daemon answers with ``setup_state``. All features
    default off, and every judgment fails closed — an unavailable or
    low-confidence answer is the unjudged behavior, never a block.
    Running sessions keep the wiring they started with; the toggles
    take effect on the next session.
    """

    type: Literal["set_judgments"] = "set_judgments"
    verification: bool
    semantic_breaker: bool
    candidate_selection: bool
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_state_chars: int = Field(default=2000, ge=100, le=50_000)
    # TD-708: the connector. ``typesafe`` needs its keychain key; the
    # daemon falls back to ``worker`` when no key is stored.
    backend: Literal["worker", "typesafe"] = "worker"
    # Which stored credential feeds the TypeSafe connector. ``None``
    # keeps the current binding.
    typesafe_credential: str | None = None


class SetCoworker(ClientMessage):
    """Turn coworker mode on or off (TD-2905).

    Machine-wide, no session. Persists ``{user_data_dir}/coworker.yaml``.
    The daemon answers with ``setup_state``. Default on.
    """

    type: Literal["set_coworker"] = "set_coworker"
    enabled: bool


class SetVoice(ClientMessage):
    """Turn hold-to-talk dictation on or off (TD-4701).

    Machine-wide, no session. Persists ``{user_data_dir}/voice.yaml``.
    The daemon answers with ``setup_state``. Default off. No always-on mic.
    """

    type: Literal["set_voice"] = "set_voice"
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


class SetCuPolicy(ClientMessage):
    """Background vs full-control computer use, denylist, master switch (TD-4830).

    Machine-wide, no session. Persists ``~/.tst-cu-mcp/config.yaml`` so the
    MCP server re-reads it on the next tool call. Acked with ``setup_state``.
    """

    type: Literal["set_cu_policy"] = "set_cu_policy"
    enabled: bool
    mode: Literal["background", "full_control"]
    unhide_on_finish: bool
    denied_apps: list[str] = Field(default_factory=list)


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


class RotateRemoteToken(ClientMessage):
    """Replace the phone token and drop remote clients (TD-5005).

    Machine-wide, no session. A no-op when remote attach is off. The
    daemon answers with ``setup_state``. The new token is never sent on
    the wire: it is read from ``{user_data_dir}/remote-token``.
    """

    type: Literal["rotate_remote_token"] = "rotate_remote_token"


class StartPairing(ClientMessage):
    """Show a pairing code for a new phone (TD-5014). Loopback only.

    Machine-wide, no session. Turns remote attach on first when it is off,
    and back off when that still binds no Tailscale address. Answered with
    ``pairing_code``, or an error (never with a ``session_id``):
    ``pairing_unavailable`` when no Tailscale address can be bound or the
    paired devices file cannot be read, ``pairing_limit`` when full. A new
    start replaces the code.
    """

    type: Literal["start_pairing"] = "start_pairing"


class CancelPairing(ClientMessage):
    """Drop the active pairing code (TD-5014). Loopback only; acked with ``setup_state``."""

    type: Literal["cancel_pairing"] = "cancel_pairing"


class RemoveRemoteDevice(ClientMessage):
    """Forget one paired device and close its connections (TD-5014).

    Loopback, or a phone naming the device it authenticated as ("Forget
    this computer"); any other id from a remote peer is ``loopback_only``.
    Acked with ``setup_state`` before the connections close, or the error
    ``remove_failed`` when the file cannot be written (the connections
    close anyway). An unknown id changes nothing.
    """

    type: Literal["remove_remote_device"] = "remove_remote_device"
    device_id: str = Field(min_length=1)


class AddMachine(ClientMessage):
    """Pair another computer so its sessions show here (TD-5015). Loopback only.

    Either ``link`` (a ``tstdesk://pair`` link) or ``host``, ``port`` and
    ``code``. The host must be a Tailscale address or a ``*.ts.net`` name.
    Runs in the background; the answer is a pushed ``machine_list`` or an
    error: ``pair_failed``, ``pair_locked``, ``machine_unreachable``,
    ``not_tailscale``, ``machine_limit``, ``machine_busy`` or
    ``keychain_locked``. The code is never logged or echoed.
    """

    type: Literal["add_machine"] = "add_machine"
    link: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    code: str | None = None


class RemoveMachine(ClientMessage):
    """Forget a paired computer (TD-5015). Loopback only. Answered with ``machine_list``.

    Best effort, the hub first removes itself from that computer's
    paired devices. ``machine_not_found`` for an unknown id.
    """

    type: Literal["remove_machine"] = "remove_machine"
    machine: str = Field(min_length=1)


class ListMachines(ClientMessage):
    """The paired computers as ``machine_list`` (TD-5015). Loopback or a viewer."""

    type: Literal["list_machines"] = "list_machines"


class ReconnectMachine(ClientMessage):
    """Try a paired computer again now, resetting the backoff (TD-5015). Loopback only."""

    type: Literal["reconnect_machine"] = "reconnect_machine"
    machine: str = Field(min_length=1)


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


class StopTurn(ClientMessage):
    """Stop the in-flight turn and leave the session open (TD-5003).

    The composer stop control sends this. ``cancel`` still ends the
    session. A session with nothing in flight ignores it.
    """

    type: Literal["stop_turn"] = "stop_turn"
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


class SteeringAccept(ClientMessage):
    """Write a parked steering proposal as proposed (TD-5140).

    The daemon writes the file, never ``fs_write``. Loopback only.
    """

    type: Literal["steering_accept"] = "steering_accept"
    session_id: str
    proposal_id: str


class SteeringEdit(ClientMessage):
    """Write a parked steering proposal with the user's edited text (TD-5140)."""

    type: Literal["steering_edit"] = "steering_edit"
    session_id: str
    proposal_id: str
    content: str


class SteeringReject(ClientMessage):
    """Reject a steering proposal. Writes nothing; logged (TD-5140)."""

    type: Literal["steering_reject"] = "steering_reject"
    session_id: str
    proposal_id: str


class EndSession(ClientMessage):
    """Run distill for one session (TD-2302). Not a kill."""

    type: Literal["end_session"] = "end_session"
    session_id: str


class Shutdown(ClientMessage):
    """Ask the daemon to shut down cleanly (sent by the supervising host)."""

    type: Literal["shutdown"] = "shutdown"


class Drain(ClientMessage):
    """Let open turns finish ahead of a restart (TD-5023). Loopback only.

    New turns on this computer's sessions are refused with
    ``daemon_draining`` while the daemon waits up to ``timeout_s`` for the
    open ones, and for ``hold_s`` after, so the restart that follows cuts
    nothing. The hold ends by itself. ``timeout_s=0, hold_s=0`` releases
    a hold at once. Answered with ``drain_result``.
    """

    type: Literal["drain"] = "drain"
    timeout_s: int = Field(default=30, ge=0, le=600)
    hold_s: int = Field(default=120, ge=0, le=600)


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
    """Store an API key in the OS keychain (TD-1101, TD-1717, TD-1722).

    The key never appears in any event, log, or audit record — the ack is a
    refreshed ``setup_state`` event whose ``has_api_key`` flips true.
    ``credential`` is the catalog id (default ``openrouter``). ``name``
    creates or updates the display name; omitted keeps the existing name
    or titles the id. ``base_url`` is the OpenAI-compatible host this key
    talks to (TD-1722). Omitted leaves or inherits; empty clears so the
    preset URL stays in charge.
    """

    type: Literal["set_api_key"] = "set_api_key"
    api_key: str = Field(min_length=1)
    credential: str | None = None
    name: str | None = None
    base_url: str | None = None


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


class SetEngine(ClientMessage):
    """Pick the agent engine for new sessions (native TST loop or Grok ACP)."""

    type: Literal["set_engine"] = "set_engine"
    kind: Literal["native", "grok"]


class SetGrokMode(ClientMessage):
    """Switch the live Grok ACP session mode (plan / default / yolo)."""

    type: Literal["set_grok_mode"] = "set_grok_mode"
    session_id: str
    mode: str = Field(min_length=1)


class RunGrokCommand(ClientMessage):
    """Send a Grok slash command as the next prompt (``/{name} {argument}``)."""

    type: Literal["run_grok_command"] = "run_grok_command"
    session_id: str
    name: str = Field(min_length=1)
    argument: str = ""


class ListGrokSessions(ClientMessage):
    """Ask for TUI sessions stored under ``~/.grok/sessions``."""

    type: Literal["list_grok_sessions"] = "list_grok_sessions"


class OpenInTerminal(ClientMessage):
    """Open the real Grok TUI on this session's Grok id (``grok --resume``)."""

    type: Literal["open_in_terminal"] = "open_in_terminal"
    session_id: str


class ApproveGrokPlan(ClientMessage):
    """Tell Grok to approve the current plan and start implementing."""

    type: Literal["approve_grok_plan"] = "approve_grok_plan"
    session_id: str
    comment: str = ""


class ListGrokExtensions(ClientMessage):
    """Ask for MCP / skills / plugins discovered in ``~/.grok`` (no secrets)."""

    type: Literal["list_grok_extensions"] = "list_grok_extensions"


class SetSpendCap(ClientMessage):
    """Set or clear this workspace's session spend budget.

    ``spend_usd`` omitted or null removes ``caps.spend_usd`` from
    ``.tst/config.yaml``. The built-in $25 pause still applies and the
    title bar does not show a budget. A number replaces that pause and
    is what the title bar prints. Acked with ``boundary_update``.
    """

    type: Literal["set_spend_cap"] = "set_spend_cap"
    session_id: str
    spend_usd: float | None = None

    @field_validator("spend_usd")
    @classmethod
    def _spend_non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("spend_usd must be >= 0")
        return value


class TrustWorkspaceConfig(ClientMessage):
    """Trust or decline the session's workspace config (TD-5113).

    ``sha256`` is the hash the trust card showed (``boundary_update``
    ``config_trust.sha256``). A file that changed since is refused with
    ``workspace_config_changed`` and re-announced, so a decision only ever
    covers the bytes the user read. The decision applies to every live
    session on that workspace and is acked with their ``boundary_update``.
    Only this computer's own window may send it.
    """

    type: Literal["trust_workspace_config"] = "trust_workspace_config"
    session_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust: bool


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
    renames that catalog row. ``base_url`` sets the host that key talks to
    (TD-1722): omitted leaves an existing host, empty clears it so the
    preset URL stays in charge. Acked with ``setup_state``.
    """

    type: Literal["set_credential"] = "set_credential"
    name: str = Field(min_length=1, max_length=40)
    credential: str | None = None
    base_url: str | None = None


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


def _refuse_mcp_values(value: Any) -> Any:
    """Keep typed values off the server messages; they would land in config."""
    if isinstance(value, dict):
        for key in ("env", "environment", "headers"):
            if key in value:
                raise ValueError(
                    "MCP server messages cannot carry env or headers; tokens stay in "
                    "the keychain (use set_mcp_secret)"
                )
    return value


class SetMcpServer(ClientMessage):
    """Create or replace one listed MCP server (TD-4403).

    Command is argv tokens only. There is no ``env`` or ``headers`` field —
    a value typed here would land in ``config.yaml``. Extra keys are
    forbidden so they cannot sneak in later; ``set_mcp_secret`` puts a
    value in the keychain instead (TD-5110), and the server's existing
    ``env`` / ``headers`` are kept. Acked with ``setup_state``. Live
    sessions keep their previous tool set until a new session; hot-attach
    is out of scope.
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
        return _refuse_mcp_values(value)

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
        return _refuse_mcp_values(value)


class SetMcpSecret(ClientMessage):
    """Store one env var or header of a listed MCP server in the keychain (TD-5110).

    ``target`` is ``env`` for a stdio server and ``header`` for an http
    one. The value goes to the keychain item ``tst-mcp.<server_id>.<entry>``
    and ``config.yaml`` gets only ``keychain:<entry>``; a name that already
    names an entry keeps it. The value never appears in any event, log or
    audit record. Acked with ``setup_state``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_mcp_secret"] = "set_mcp_secret"
    server_id: str = Field(min_length=1)
    target: Literal["env", "header"]
    name: str = Field(min_length=1)
    value: str = Field(min_length=1, repr=False)


class DeleteMcpSecret(ClientMessage):
    """Remove one env var or header from a listed MCP server (TD-5110).

    Drops the name from ``config.yaml`` and, when it named a keychain
    entry, deletes that item. Acked with ``setup_state``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["delete_mcp_secret"] = "delete_mcp_secret"
    server_id: str = Field(min_length=1)
    target: Literal["env", "header"]
    name: str = Field(min_length=1)


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


# One ``artifact_chunk`` frame carries at most this many file bytes. As
# base64 in JSON that is about 700 KB, under the 1 MiB frame limit every
# websockets hop keeps by default (TD-5143).
CHUNK_MAX = 524_288


class ReadArtifactChunk(ClientMessage):
    """Read part of a file the agent sent (TD-5143). Answered with ``artifact_chunk``.

    Only ``kind == "sent_file"`` artifacts, by id. No path is accepted, so
    this cannot become a "read anything" verb. ``length`` is capped at
    ``CHUNK_MAX`` so one reply fits a websockets frame on every hop.
    """

    type: Literal["read_artifact_chunk"] = "read_artifact_chunk"
    session_id: str = Field(min_length=1)
    artifact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    offset: int = Field(ge=0)
    length: int = Field(default=CHUNK_MAX, ge=1, le=CHUNK_MAX)


class DesignHitTest(ClientMessage):
    """Ask the last computer-use surface what is at a CSS-pixel point.

    Browser DOM (TD-3403) or desktop AX / UIA / AT-SPI (TD-3406). Observe
    only — never actuates. The reply is connection-scoped ``design_hit``,
    not a session-log event.
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


class ResetCuPermissions(ClientMessage):
    """Reset this app's macOS TCC grants and re-request them (TD-4823).

    Connection-scoped.  The host runs ``tccutil reset`` for Screen
    Recording and Accessibility, forgets its prompt stamps, raises both
    prompts again, and the daemon answers with a fresh ``cu_permissions``.
    A user action from the window only — never exposed as a tool.  Off
    macOS (no host socket) this is just a re-probe.
    """

    type: Literal["reset_cu_permissions"] = "reset_cu_permissions"


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
    """Create or replace a scheduled job (TD-3805, TD-3810).

    Draft fields, not a natural-language parse. An existing ``id`` updates
    that job and keeps its run receipt. On that update, an omitted cadence
    or next run keeps the stored value, and an empty string clears it so
    the job can switch between recurring and one-shot. Pause is this verb
    with ``paused`` set. Firing a job is ``run_job`` or the scheduler tick,
    never this verb. Acked with ``job_list``.
    """

    type: Literal["save_job"] = "save_job"
    id: str | None = None
    workspace: str | None = None
    instruction: str | None = None
    # On an update, None keeps the stored value and "" clears it (TD-3810).
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy", "email"] | None = None
    paused: bool = False
    # IANA name ("America/Chicago") a cron cadence is read in. Omitted on an
    # edit keeps the job's current zone; None on create means UTC. The pane
    # sends one on edit only for a legacy job whose cadence text changed.
    timezone: str | None = None
    # Catalog preset name and engine kind (TD-3812). None on create uses
    # whatever the window is using. On an edit, None keeps the stored pin
    # and "" clears it back to that. The name only — never a slug, URL, or
    # key. ``engine`` is ``native`` or ``grok``; "" clears and is not a kind,
    # so the field stays a string until the job normalizes it.
    preset: str | None = None
    engine: str | None = None
    # How late a slot may be and still run (TD-3813). A phrase the
    # scheduler already understands ("2 hours", "30 minutes", "every 2
    # hours") or a positive number of seconds. The job stores seconds.
    # On an edit, None keeps the stored grace and "" clears it back to
    # always running a late slot once. Omitted on create means that too.
    grace: str | int | None = None
    # Extra tries after a transient scheduled failure (TD-3814). 0-3.
    # On an edit, None keeps the stored count. Omitted on create means
    # never retry. Run now ignores this. 0 clears a count the way ""
    # clears grace.
    retries: int | None = None
    # Gap between those tries. A phrase ("10 minutes") or seconds. On an
    # edit, None keeps the stored delay and "" means the 10-minute default
    # when retries is at least 1. Omitted on create with retries set is
    # that default. Ignored when retries is 0.
    retry_delay: str | int | None = None
    # How long this job's turn may run (TD-3819). A phrase ("20 minutes")
    # or a number of seconds, stored as seconds, from 1 to 60 minutes.
    # On an edit, None keeps the stored limit and "" clears it so the
    # job uses scheduler.max_run_seconds. Omitted on create means that too.
    max_run: str | int | None = None
    # Another job's id to start once after this one ends ok (TD-3817).
    # On an edit, None keeps the stored id and "" clears it. Omitted on
    # create means no follow-on. A cycle is refused at save. The child's
    # own schedule is not moved when it is started this way.
    then: str | None = None
    # Absolute path of a local .ics file (TD-3818). On an edit, None keeps
    # the stored path and "" clears it. Omitted on create means no calendar.
    # The file must exist when the path is set or changed. Pause omits it.
    skip_calendar: str | None = None
    # Case-insensitive substring, or several joined by "|". Blank matches
    # every event. On an edit, None keeps the stored list and "" clears it
    # back to every event. Omitted on create means every event.
    skip_match: str | None = None
    # One address when ``deliver_to`` is email (TD-3820). On an edit, None
    # keeps the stored address and "" clears it. Omitted on create with
    # another channel means none. Pause omits it.
    email_to: str | None = None


class DeleteJob(ClientMessage):
    """Remove a scheduled job by id (TD-3805).

    Also removes that job's run history (TD-3811) and clears ``then`` on
    any job that pointed at this id (TD-3817). Acked with ``job_list``.
    Unknown id is a typed error. Does not run anything.
    """

    type: Literal["delete_job"] = "delete_job"
    job_id: str = Field(min_length=1)


class ParseJob(ClientMessage):
    """Turn natural language into a job draft (TD-3803).

    Does not persist. The daemon answers with ``job_draft`` for the user
    to edit before ``save_job``. The parse is the worker-shaped schema
    (deterministic); it is not a model call and not a save.
    """

    type: Literal["parse_job"] = "parse_job"
    text: str = ""


class RunJob(ClientMessage):
    """Fire one scheduled job now (TD-3809).

    Connection-scoped. The daemon starts the turn as its own task and
    answers at once with ``job_list`` (``running`` set on that row). A
    second ``job_list`` is pushed when the turn finishes. Does not move
    ``next_run`` and does not change ``paused``, including on a job that
    is already paused. Unknown id is ``job_not_found``; a job already in
    flight is ``job_running``.
    """

    type: Literal["run_job"] = "run_job"
    job_id: str = Field(min_length=1)


class ListJobRuns(ClientMessage):
    """List one job's run history (TD-3811).

    Connection-scoped. The daemon answers with ``job_runs``, newest first.
    Unknown id is ``job_not_found``. Does not run the job. History is not
    part of ``job_list``: that push already happens on every fire, and a
    capped log does not belong on every pause.
    """

    type: Literal["list_job_runs"] = "list_job_runs"
    job_id: str = Field(min_length=1)


class ListJobTemplates(ClientMessage):
    """List built-in and user job templates (TD-3816).

    Connection-scoped. The daemon answers with ``job_templates``. Does not
    create a job or a template file.
    """

    type: Literal["list_job_templates"] = "list_job_templates"


class SaveJobTemplate(ClientMessage):
    """Store the current job form as a new template (TD-3816).

    Create only. The daemon assigns the id, so a client cannot overwrite a
    built-in. An empty instruction is allowed: the weekday digest ships that
    way. Acked with ``job_templates``. Does not create a job. ``name`` is
    not ``min_length`` here — an empty or secret-shaped name is refused by
    the template check, whose message does not echo the text.
    """

    type: Literal["save_job_template"] = "save_job_template"
    name: str = ""
    instruction: str = ""
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy", "email"] | None = None
    grace: str | int | None = None
    retries: int | None = None
    retry_delay: str | int | None = None
    # Same phrases as a job. Omitted means the config limit (TD-3819).
    max_run: str | int | None = None
    preset: str | None = None
    engine: str | None = None
    workspace: str | None = None
    # One address when ``deliver_to`` is email (TD-3820). Omitted means none.
    email_to: str | None = None


class DeleteJobTemplate(ClientMessage):
    """Remove a user job template (TD-3816).

    Built-ins answer ``template_builtin`` and are not removed. Unknown id
    is ``template_not_found``. Acked with ``job_templates``.
    """

    type: Literal["delete_job_template"] = "delete_job_template"
    template_id: str = ""


class GetSessionJobSource(ClientMessage):
    """Read one chat into a job draft (TD-3816).

    Connection-scoped. The daemon answers with ``session_job_source``.
    ``session_list`` has no engine and no full first message, so this is
    the read. It does not create a job. Unknown id is ``session_not_found``.
    """

    type: Literal["get_session_job_source"] = "get_session_job_source"
    session_id: str = ""


class Transcribe(ClientMessage):
    """Hold-to-talk audio for speech-to-text (TD-4701).

    Used when Settings has dictation on. The daemon POSTs to
    ``voice.base_url`` or ``speech.base_url`` and answers with
    ``transcript``. ``audio_b64`` is the recording's bytes. There is no
    cloud default; OS dictation never reaches this verb.
    """

    type: Literal["transcribe"] = "transcribe"
    audio_b64: str = Field(min_length=1, max_length=2_800_000)
    mime: str = Field(default="audio/webm", min_length=1, max_length=128)


class SetEmailNotify(ClientMessage):
    """Save email settings (TD-3820, TD-4854). Acked with ``setup_state``.

    ``password`` is written to the keychain and never stored on
    ``setup_state``. None or "" leaves the stored password alone.
    ``google_client_secret`` is the same for the desktop client secret.
    """

    type: Literal["set_email_notify"] = "set_email_notify"
    enabled: bool = False
    method: Literal["smtp", "microsoft", "google"] = "smtp"
    host: str = ""
    port: int = 587
    security: Literal["starttls", "tls"] = "starttls"
    username: str = ""
    from_address: str = ""
    password: str | None = None
    # Desktop client secret (TD-4854). Written to the keychain. None or ""
    # leaves the stored secret alone. Never echoed on setup_state.
    google_client_secret: str | None = None


class TestEmail(ClientMessage):
    """Send one short test to ``to`` (TD-3820). Acked with ``email_test_result``."""

    type: Literal["test_email"] = "test_email"
    to: str = ""


class GithubConnect(ClientMessage):
    """Start GitHub device flow (TD-4852). Acked with ``github_status``.

    The device code stays in the daemon. The reply carries the user code
    and the verification URL only.
    """

    type: Literal["github_connect"] = "github_connect"


class GithubDisconnect(ClientMessage):
    """Delete the GitHub keychain item (TD-4852). Acked with ``github_status``."""

    type: Literal["github_disconnect"] = "github_disconnect"


class MicrosoftConnect(ClientMessage):
    """Start Microsoft sign-in (TD-4853). Acked with ``microsoft_status``.

    The daemon opens the browser and listens on 127.0.0.1. The code, the
    verifier, and the tokens stay in the daemon.
    """

    type: Literal["microsoft_connect"] = "microsoft_connect"


class MicrosoftDisconnect(ClientMessage):
    """Delete the Microsoft keychain item (TD-4853). Acked with ``microsoft_status``."""

    type: Literal["microsoft_disconnect"] = "microsoft_disconnect"


class GoogleConnect(ClientMessage):
    """Start Google sign-in (TD-4854). Acked with ``google_status``.

    The daemon opens the browser and listens on 127.0.0.1. The code, the
    verifier, the client secret, and the tokens stay in the daemon.
    """

    type: Literal["google_connect"] = "google_connect"


class GoogleDisconnect(ClientMessage):
    """Delete the Google token keychain item (TD-4854). Acked with ``google_status``.

    The desktop client secret is a different item and is left in place.
    """

    type: Literal["google_disconnect"] = "google_disconnect"


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
    # Agent engine this session is running. Additive; older clients ignore
    # it. None on a log event written before the field existed — the UI
    # must not invent native/grok from that silence. New events always
    # stamp the loop the session actually started with.
    engine: Literal["native", "grok"] | None = None


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


class SteerPoint(BaseModel):
    """Where a message sent mid-turn joined the running turn (TD-5112).

    ``turn_id`` is the running turn's own ``user_turn`` id. ``step`` counts
    the model calls that turn had made, and ``after_tool_call_ids`` is the
    tool batch whose results the model read just before this message.
    """

    turn_id: str
    step: int = Field(ge=1)
    after_tool_call_ids: list[str] = Field(default_factory=list)


class UserTurn(DaemonEvent):
    """A user message the loop has accepted (needed for honest replay).

    The client echoes locally on send. This event exists so a restarted
    daemon can replay the user's side of the transcript without inventing
    it. ``turn_id`` lets a live echo and the replayed event be the same
    row rather than a duplicate.

    ``steer`` is set when the message was sent while a turn ran and the
    loop handed it to that turn at a tool-batch boundary (TD-5112): no
    new turn starts, and the model reads it before its next step.
    Additive; ``None`` for a message that starts a turn.
    """

    type: Literal["user_turn"] = "user_turn"
    session_id: str
    turn_id: str
    content: str
    steer: SteerPoint | None = None


class QueuedMessageEntry(BaseModel):
    """One message a restart left held (TD-5155).

    ``content`` is the text the ``user_turn`` will show. ``images_dropped``
    counts attached images that were not kept: image bytes are never
    written to the queue file, so a held message goes out without them.
    """

    queued_id: str
    content: str
    images_dropped: int = 0


class QueuedMessages(DaemonEvent):
    """The session's held messages, in order, whole each time (TD-5155).

    A message the daemon accepted but no turn had started when it stopped
    comes back here instead of being sent: the person picks ``send_queued``
    or ``remove_queued``. Logged when the list changes and after a restart
    that left any, so the latest one in a replay is the list.
    """

    type: Literal["queued_messages"] = "queued_messages"
    session_id: str
    messages: list[QueuedMessageEntry] = Field(default_factory=list)


class AssistantDelta(DaemonEvent):
    """A streamed chunk of assistant output.

    ``response_id`` (TD-5159, additive) names the model response the text
    belongs to, so an ``assistant_retract`` can withdraw exactly that
    response. Lines the daemon writes itself (a failure, a progress note)
    and older daemons leave it ``None``; such text is never withdrawn.
    """

    type: Literal["assistant_delta"] = "assistant_delta"
    session_id: str
    delta: str
    response_id: str | None = None


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
    # The model response this thinking belongs to (TD-5159), as on
    # ``AssistantDelta``.
    response_id: str | None = None


class AssistantRetract(DaemonEvent):
    """Withdraw one model response's shown text and thinking (TD-5159).

    The provider went quiet partway through the reply (``reason`` is the
    error code, ``stream_stalled``), and the loop asks again. Every
    ``assistant_delta`` and ``assistant_reasoning`` carrying this
    ``response_id`` is no longer part of the reply: a client removes it,
    and the retry streams under a new id. Nothing else on the row goes:
    tool calls that already ran, and text from earlier responses in the
    turn, stay. The partial text never entered the model's conversation.
    A client that does not know this event drops it and keeps showing the
    withdrawn text above the retry.
    """

    type: Literal["assistant_retract"] = "assistant_retract"
    session_id: str
    response_id: str
    reason: str


class ToolCall(DaemonEvent):
    """A tool call about to be executed."""

    type: Literal["tool_call"] = "tool_call"
    session_id: str
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    decision_class: Literal["A", "B", "C"] | None = None


class WorkerStep(BaseModel):
    """One step of a delegated worker's run, for display only (TD-5117).

    The worker's transcript rides on the ``delegate`` call's
    ``tool_result`` so the window can show it. The parent model never
    sees it: its tool message is the capped summary in ``output``.
    ``text`` is cut to a few thousand characters per step, and
    ``truncated`` says so.
    """

    kind: Literal["task", "reply", "tool_call", "tool_result", "note"]
    text: str
    tool: str | None = None
    status: Literal["success", "error"] | None = None
    truncated: bool = False


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
    # The read-only shell list entries that let the call run without
    # asking (TD-5103). Empty when anything else decided: a policy rule,
    # an approval, or another class default. The audit row keeps them.
    allowed_by: list[str] = Field(default_factory=list)
    # A ``delegate`` call's worker transcript (TD-5117), for display.
    # Empty for every other tool.
    transcript: list[WorkerStep] = Field(default_factory=list)


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


class ToolProgress(DaemonEvent):
    """Progress a long tool reported while it runs (TD-5105).

    MCP servers send ``notifications/progress`` against the call's token.
    ``total`` is absent when the server does not know it.  Events are
    thinned to two a second per call.
    """

    type: Literal["tool_progress"] = "tool_progress"
    session_id: str
    tool_call_id: str
    progress: float
    total: float | None = None
    message: str | None = None


class PolicyRuleSummary(BaseModel):
    """A saved policy rule, surfaced to clients (TD-803).

    ``(tool, args)`` is the identity used for individual revocation in
    settings.  ``exact`` (TD-5149) is the one text ``args`` matches when it
    has no wildcard, with Always allow's escapes read back
    (``app/[[]id]/page.tsx`` → ``app/[id]/page.tsx``), so the window can
    show the path a rule is for; ``None`` for a glob.
    """

    tool: str
    args: str
    effect: Literal["auto", "ask", "never"]
    exact: str | None = None


class ApprovalRequest(DaemonEvent):
    """A request for user approval of a tool call (TD-802).

    ``summary`` is the human-readable action ("Run `npm test`");
    ``reason`` is why approval is required ("decision class B requires
    approval", "policy rule `shell: rm *` → ask").

    ``proposed_always_allow`` (TD-803) is the rule that "always allow in
    this workspace" would write, or ``None`` when the call is a class-C
    decision that can never be always-allowed, or when the workspace config
    is not trusted and no rule could be saved into it (TD-5113), or when an
    ``ask`` or ``never`` rule with the same ``(tool, args)`` is saved and
    Always allow would replace it (TD-5149).  The client shows it before
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


class VerifyStarted(DaemonEvent):
    """An after-write review started running (TD-5158).

    It runs after ``turn_complete`` and holds the loop until its
    ``verify_result``; Stop ends it (TD-5151). The window offers Stop in
    that span. Not shown in the timeline.
    """

    type: Literal["verify_started"] = "verify_started"
    session_id: str


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
    # Active tier accepts image attachments (TD-4705). Default false.
    vision: bool = False


class LoosenedSetting(BaseModel):
    """One setting a workspace config loosens, for the trust card (TD-5113)."""

    key: str
    value: str
    default: str


class ConfigTrust(BaseModel):
    """Whether the workspace's ``.tst/config.yaml`` is in force (TD-5113).

    ``pending``: it loosens something and the user has not decided on
    this hash; ``declined``: they kept the defaults. Both run on packaged
    defaults. ``trusted``: it applies.
    """

    state: Literal["pending", "trusted", "declined"]
    sha256: str
    loosened: list[LoosenedSetting]


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
    # True when this workspace's file sets caps.spend_usd, or a charter
    # supplies the cap. False means the built-in pause, which the title
    # bar does not present as the user's budget. Omitted by older daemons.
    spend_configured: bool = False
    # Attachment caps (TD-1709), so the composer refuses oversize files
    # against this workspace's real numbers instead of a hardcoded guess.
    # Additive with a default: a client that ignores it behaves as it did.
    attachments: AttachmentLimits = Field(default_factory=AttachmentLimits)
    # TD-5113: set when the workspace config loosens anything, so the
    # window can ask before it applies. None when there is nothing to ask.
    config_trust: ConfigTrust | None = None


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
    exactly what compaction did.

    ``trigger: "inflight_trim"`` (TD-5150) is not compaction: the loop
    shortened the middles of this turn's tool results (TD-5010) and no
    message left the conversation.  ``dropped_messages`` then counts the
    tool results trimmed, and ``kept_messages`` is every message.  The
    window shows it as a quieter "Trimmed tool output" row.

    ``trigger: "clear_stale"`` (TD-5115) is not compaction either: tool
    results from earlier turns were replaced with a one-line placeholder
    before any turn had to be compacted.  ``dropped_messages`` counts the
    results cleared and ``kept_messages`` is every message.  The window
    shows it as the same quiet row, "Cleared earlier tool results"."""

    type: Literal["context_compacted"] = "context_compacted"
    session_id: str
    dropped_messages: int = Field(ge=0)
    kept_messages: int = Field(ge=0)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    #: What compacted (TD-5109): the budget (``auto``), ``/compact`` or
    #: the gauge (``manual``), or "Summarize from here" (``from_here``).
    #: ``inflight_trim`` is the in-turn tool-result trim (TD-5150), and
    #: ``clear_stale`` the clearing of earlier turns' results (TD-5115).
    trigger: Literal["auto", "manual", "from_here", "inflight_trim", "clear_stale"] = "auto"
    guidance: str | None = None


class ContextUsage(DaemonEvent):
    """The prompt the next model call sends, against the tier's window
    (TD-5109).  ``used_tokens`` is the daemon's calibrated estimate
    (TD-5013), tool definitions included.  ``compact_at`` is where
    compaction triggers.  ``tier`` is the role (brain, worker,
    validator), as ``TierState`` names it.  Sent before each model call
    and after a compaction."""

    type: Literal["context_usage"] = "context_usage"
    session_id: str
    tier: str
    used_tokens: int = Field(ge=0)
    context_window: int = Field(gt=0)
    compact_at: int = Field(ge=0)


TaskStatus = Literal["pending", "in_progress", "done"]


class TaskListItem(BaseModel):
    """One item on the native loop's task list (TD-5122)."""

    content: str = Field(min_length=1, max_length=200)
    status: TaskStatus = "pending"


class TaskList(DaemonEvent):
    """The session's whole task list, after a ``task_list`` call (TD-5122).

    The native loop's counterpart of ``grok_plan``. Each event carries the
    full list, so the newest one is the truth; an empty list clears it.
    """

    type: Literal["task_list"] = "task_list"
    session_id: str
    items: list[TaskListItem] = Field(default_factory=list, max_length=30)


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


class SteeringFileDiff(BaseModel):
    """One file in a ``steering_proposal`` or ``steering_decision`` (TD-5140).

    ``path`` is workspace-relative. ``before`` is null when the file does
    not exist yet (``create``).
    """

    action: Literal["create", "replace"]
    path: str
    diff: str
    before: str | None = None
    after: str | None = None


class SteeringProposal(DaemonEvent):
    """The agent proposed a steering edit; nothing is written yet (TD-5140).

    Shaped like ``memory_proposal``. ``replaces`` names a stale proposal
    this fresh diff supersedes (the file changed before Accept).
    ``autonomy`` marks one an unattended run parked for its summary.
    """

    type: Literal["steering_proposal"] = "steering_proposal"
    session_id: str
    proposal_id: str
    files: list[SteeringFileDiff]
    tool_call_id: str = ""
    replaces: str | None = None
    autonomy: bool = False


class SteeringDecision(DaemonEvent):
    """The user's decision on a steering proposal, with its diff (TD-5140).

    ``accepted`` wrote the proposed text, ``edited`` wrote the user's own
    text (the diff is what was written), ``rejected`` wrote nothing.
    ``expired`` is the daemon's, not the user's: a restart dropped the open
    proposal, so it was closed when the session was restored and nothing
    was written.
    """

    type: Literal["steering_decision"] = "steering_decision"
    session_id: str
    proposal_id: str
    decision: Literal["accepted", "edited", "rejected", "expired"]
    files: list[SteeringFileDiff]


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
    # TD-5140: steering edits the run proposed, waiting for the user's
    # decision on the card. A run never accepts one by itself.
    steering_proposals: list[str] = Field(default_factory=list)


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
    # Frontmatter keys the skill carried that TST Desk did not act on;
    # the skill loaded anyway (TD-5107). Additive, defaults empty.
    ignored_keys: list[str] = Field(default_factory=list)
    # `disable-model-invocation`: kept out of the model's catalog, and
    # only the user's `/name` loads it (TD-5107).
    user_only: bool = False


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
    # The paired computer this row lives on (TD-5015), as its ``mch_`` id;
    # None is the answering daemon's own. Set by the hub, never trusted
    # from upstream. Only viewers ever see a non-None value.
    machine: str | None = None
    # Auto-title from the first non-empty user message (TD-3001). None
    # until then — the rail falls back to the short id. Additive.
    title: str | None = None
    # Catalog preset this session opened with, or last switched to
    # (TD-1721). Empty on a store written before the field. Additive.
    preset: str = ""
    # A turn is in flight (TD-1720). Distinct from ``state: running``,
    # which is loop liveness and spans the session's whole life
    # (TD-1714). Additive with a default — a client that ignores it
    # still reads the list it always did.
    busy: bool = False


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


class McpExcludedToolSummary(BaseModel):
    """An advertised tool the client left out, and why (TD-5104)."""

    name: str
    reason: str


class McpValueSummary(BaseModel):
    """One env var or header of a listed MCP server (TD-5110). Never its value.

    ``source`` is ``keychain`` for a ``keychain:<entry>`` reference, with
    ``stored`` a presence probe of that item, or ``literal`` for a value
    written in ``config.yaml`` (not shown here; ``stored`` is false).
    """

    name: str
    source: Literal["literal", "keychain"]
    entry: str = ""
    stored: bool = False


class McpServerSummary(BaseModel):
    """One listed MCP server as shown in settings (TD-4403). Never a secret."""

    id: str
    transport: Literal["stdio", "http"]
    command: list[str] = Field(default_factory=list)
    url: str = ""
    enabled: bool = True
    #: Tools left out at the last load (TD-5104). Empty until the server loads.
    excluded_tools: list[McpExcludedToolSummary] = Field(default_factory=list)
    #: Names only (TD-5110). Additive; an older client ignores them.
    env: list[McpValueSummary] = Field(default_factory=list)
    headers: list[McpValueSummary] = Field(default_factory=list)


class RemoteDeviceSummary(BaseModel):
    """One paired device as the UI may see it (TD-5014). Never a key or hash.

    Times are ``YYYY-MM-DDTHH:MM:SSZ``. ``last_seen`` is None until the
    device first connects, and is written at most once a minute.
    """

    id: str
    name: str
    paired_at: str
    last_seen: str | None = None
    connected: bool = False
    # TD-5015: "machine" is another computer's hub. A file entry written
    # before the field reads as a phone.
    kind: Literal["phone", "machine"] = "phone"


class EmailNotifyState(BaseModel):
    """Email settings on ``setup_state``. Never a password or a token."""

    enabled: bool = False
    method: Literal["smtp", "microsoft", "google"] = "smtp"
    host: str = ""
    port: int = 587
    security: Literal["starttls", "tls"] = "starttls"
    username: str = ""
    from_address: str = ""
    password_stored: bool = False


GithubPhase = Literal["off", "idle", "pending", "connected", "expired", "denied", "error"]


class GithubConnectorState(BaseModel):
    """GitHub sign-in as Settings may show it. Never a token or device code."""

    enabled: bool = False
    connected: bool = False
    phase: GithubPhase = "off"
    user_code: str = ""
    verification_uri: str = ""
    login: str | None = None
    message: str = ""


MicrosoftPhase = Literal["off", "idle", "pending", "connected", "denied", "error", "expired"]


class MicrosoftConnectorState(BaseModel):
    """Microsoft sign-in as Settings may show it. Never a token or auth code."""

    enabled: bool = False
    connected: bool = False
    phase: MicrosoftPhase = "off"
    account: str | None = None
    message: str = ""


GooglePhase = Literal["off", "idle", "pending", "connected", "denied", "error", "expired"]


class GoogleConnectorState(BaseModel):
    """Google sign-in as Settings may show it. Never a token, secret, or auth code.

    ``client_secret_stored`` is a presence probe for the desktop client secret.
    """

    enabled: bool = False
    connected: bool = False
    phase: GooglePhase = "off"
    account: str | None = None
    message: str = ""
    client_secret_stored: bool = False


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
    # TD-5005: the listener port while remote attach is bound, else None.
    remote_port: int | None = None
    # TD-5014: paired devices. Additive; kept while remote attach is off.
    remote_devices: list[RemoteDeviceSummary] = Field(default_factory=list)
    # TD-1717: named keys. Additive; an older client ignores them.
    # ``stored`` is a keychain probe, never the secret. ``tier_credentials``
    # is the configured binding (None = unbound). ``tier_loopback`` lets
    # the model picker offer "no key" only on loopback tiers.
    # ``credentials[].base_url`` is the key's host (TD-1718).
    credentials: list[CredentialSummary] = Field(default_factory=list)
    tier_credentials: dict[str, str | None] = Field(default_factory=dict)
    tier_loopback: dict[str, bool] = Field(default_factory=dict)
    # Agent engine for new sessions. Additive; older clients ignore it.
    # grok_available is a PATH probe, never a credential.
    engine: Literal["native", "grok"] = "native"
    grok_available: bool = False
    grok_binary: str | None = None
    # TD-4701: hold-to-talk. Additive, default off. voice_has_endpoint is
    # whether config.yaml names a transcription URL — never the URL itself.
    voice_enabled: bool = False
    voice_has_endpoint: bool = False
    # TD-4830: computer-use policy. Additive; older clients ignore them.
    cu_enabled: bool = True
    cu_mode: Literal["background", "full_control"] = "background"
    cu_unhide_on_finish: bool = True
    cu_denied_apps: list[str] = Field(default_factory=list)
    # TD-708/709/710/711 (dev): judgment-seam toggles. Additive; the
    # defaults match the shipped config (everything off), so an older
    # client that ignores them reads the product as unchanged.
    judgments_verification: bool = False
    judgments_semantic_breaker: bool = False
    judgments_candidate_selection: bool = False
    judgments_confidence_threshold: float = 0.6
    judgments_max_state_chars: int = 2000
    # The configured connector, its keychain credential id, and whether
    # that key is actually stored — a presence probe, never the secret.
    judgments_backend: Literal["worker", "typesafe"] = "worker"
    judgments_typesafe_credential: str = "typesafe"
    judgments_typesafe_key_stored: bool = False
    # TD-4403: listed MCP servers. Additive, default empty. Never a secret;
    # env and headers are names only (TD-5110). An older client ignores it.
    mcp_servers: list[McpServerSummary] = Field(default_factory=list)
    # TD-4701: hold-to-talk. Additive, default off. ``speech_ready`` is
    # enabled plus a configured base_url — the URL itself never leaves
    # the daemon. An older client ignores both fields.
    speech_enabled: bool = False
    speech_ready: bool = False
    # TD-3820: SMTP settings. Additive. ``password_stored`` is a presence
    # probe — the password itself never leaves the keychain.
    email: EmailNotifyState = Field(default_factory=EmailNotifyState)
    # TD-4852: GitHub connector. Additive. No token, no device code.
    github: GithubConnectorState = Field(default_factory=GithubConnectorState)
    # TD-4853: Microsoft sign-in for email. Additive. No token, no auth code.
    microsoft: MicrosoftConnectorState = Field(default_factory=MicrosoftConnectorState)
    # TD-4854: Google sign-in for email. Additive. No token, no secret, no auth code.
    google: GoogleConnectorState = Field(default_factory=GoogleConnectorState)


class PairingCode(DaemonEvent):
    """Reply to ``start_pairing`` (TD-5014). Connection-scoped; loopback only.

    ``code`` is 8 Crockford base32 characters, ``display_code`` the same
    as ``ABCD-2345``. ``link`` is ``tstdesk://pair?h=&p=&c=&n=`` and
    ``qr_svg`` a standalone black-on-white ``<svg>`` of that link.
    ``expires_at`` is ``YYYY-MM-DDTHH:MM:SSZ``, 10 minutes after the start.
    """

    type: Literal["pairing_code"] = "pairing_code"
    seq: int = 1
    code: str
    display_code: str
    host: str
    port: int
    desktop_name: str
    link: str
    qr_svg: str
    expires_at: str


class PairingCompleted(DaemonEvent):
    """A phone redeemed the code (TD-5014). Broadcast to loopback clients only."""

    type: Literal["pairing_completed"] = "pairing_completed"
    seq: int = 1
    device: RemoteDeviceSummary


MachineStatus = Literal["connected", "connecting", "unreachable", "refused", "locked", "outdated"]


class MachineSummary(BaseModel):
    """One paired computer as a viewer may see it (TD-5015). Never a key.

    ``id`` is this hub's handle (``mch_`` + 12 hex), the suffix of every
    bridged session id. ``host`` and ``port`` are None for phones.
    ``same_computer`` is a self-pair (the other end has this daemon's
    own ``machine_id``). ``detail`` says why a computer is not connected.
    """

    id: str
    name: str
    host: str | None = None
    port: int | None = None
    status: MachineStatus
    last_seen: str | None = None
    paired_at: str
    same_computer: bool = False
    detail: str | None = None


class MachineList(DaemonEvent):
    """The paired computers (TD-5015). Connection-scoped.

    The reply to the four machine verbs, and pushed to viewers when one
    changes. Never sent to a machine peer or a non-viewer remote peer.
    """

    type: Literal["machine_list"] = "machine_list"
    seq: int = 1
    machines: list[MachineSummary] = Field(default_factory=list)


class DrainResult(DaemonEvent):
    """The answer to ``drain`` (TD-5023). Connection-scoped.

    ``drained`` is true when no turn on this computer is still open.
    ``running``, ``paused`` and ``awaiting_approval`` are session ids and
    nothing else. Only ``running`` turns are still working; a turn paused
    for the person (the progress guard or a cap, TD-5155) or parked on an
    approval is waiting on the user, so it is listed, not waited on.
    ``hold_until`` is when new turns are taken again (ISO 8601 UTC), or
    None when no hold is up.
    """

    type: Literal["drain_result"] = "drain_result"
    seq: int = 1
    drained: bool
    running: list[str] = Field(default_factory=list)
    paused: list[str] = Field(default_factory=list)
    awaiting_approval: list[str] = Field(default_factory=list)
    hold_until: str | None = None


class GrokCommand(BaseModel):
    """One slash command advertised by the Grok ACP agent."""

    name: str
    description: str = ""


class GrokCommands(DaemonEvent):
    """Slash commands the live Grok session advertised."""

    type: Literal["grok_commands"] = "grok_commands"
    session_id: str
    commands: list[GrokCommand] = Field(default_factory=list)


class GrokPlanEntry(BaseModel):
    content: str
    status: str = "pending"


class GrokPlan(DaemonEvent):
    """Plan mode document / entries from ACP ``plan`` updates."""

    type: Literal["grok_plan"] = "grok_plan"
    session_id: str
    markdown: str = ""
    entries: list[GrokPlanEntry] = Field(default_factory=list)


class GrokMode(DaemonEvent):
    """Current Grok ACP session mode and the advertised set."""

    type: Literal["grok_mode"] = "grok_mode"
    session_id: str
    mode: str
    modes: list[str] = Field(default_factory=list)
    # Model the Grok CLI is using for this session. Additive; older
    # clients ignore it. None until ACP or Grok's config names one —
    # never a guessed slug.
    model: str | None = None


class GrokPreview(DaemonEvent):
    """A media file or loopback URL the Grok engine wants shown."""

    type: Literal["grok_preview"] = "grok_preview"
    session_id: str
    kind: Literal["image", "video", "html", "pdf", "url"]
    path: str | None = None
    url: str | None = None
    title: str = ""


class GrokSessionEntry(BaseModel):
    id: str
    title: str
    cwd: str = ""
    updated_at: str = ""


class GrokSessionList(DaemonEvent):
    """TUI sessions on disk under ``~/.grok/sessions``."""

    type: Literal["grok_session_list"] = "grok_session_list"
    seq: int = 1
    sessions: list[GrokSessionEntry] = Field(default_factory=list)


class GrokExtension(BaseModel):
    kind: Literal["mcp", "skill", "plugin"]
    name: str
    detail: str = ""


class GrokExtensions(DaemonEvent):
    """Read-only catalog of Grok CLI extensions. Never secrets."""

    type: Literal["grok_extensions"] = "grok_extensions"
    seq: int = 1
    items: list[GrokExtension] = Field(default_factory=list)


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
    """One artifact on ``artifact_list`` (TD-3201). Path, not bytes.

    ``kind == "sent_file"`` (TD-5143) is a copy the agent sent with
    ``send_file``: it always carries ``name``, ``size``, ``sha256`` and
    ``tool_call_id``, and its ``path`` is the folder ``artifacts/<id>``,
    which clients never open. The bytes come from ``read_artifact_chunk``.
    """

    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str
    kind: Literal["artifact", "sent_file"] = "artifact"
    name: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    caption: str | None = None
    tool_call_id: str | None = None


class ArtifactReady(DaemonEvent):
    """An artifact was recorded for this session (TD-3201).

    Session-scoped: it rides the event log so attach can replay the
    notice.  ``list_artifacts`` is the source of truth after a trim.
    The optional fields are the ``sent_file`` ones on ``ArtifactEntry``.
    """

    type: Literal["artifact_ready"] = "artifact_ready"
    session_id: str
    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str
    kind: Literal["artifact", "sent_file"] = "artifact"
    name: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    caption: str | None = None
    tool_call_id: str | None = None


class ArtifactList(DaemonEvent):
    """Response to ``list_artifacts`` (TD-3201). Connection-scoped."""

    type: Literal["artifact_list"] = "artifact_list"
    seq: int = 1
    session_id: str
    artifacts: list[ArtifactEntry] = Field(default_factory=list)


class Artifact(DaemonEvent):
    """Response to ``open_artifact`` (TD-3201): metadata and path.

    Connection-scoped.  Bytes stay on disk; the client opens the path.
    The optional fields are the ``sent_file`` ones on ``ArtifactEntry``.
    """

    type: Literal["artifact"] = "artifact"
    seq: int = 1
    session_id: str
    artifact_id: str = Field(min_length=1)
    title: str
    mime: str
    path: str
    kind: Literal["artifact", "sent_file"] = "artifact"
    name: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    caption: str | None = None
    tool_call_id: str | None = None


class ArtifactChunk(DaemonEvent):
    """Bytes for ``read_artifact_chunk`` (TD-5143). Connection-scoped, never logged.

    ``data_b64`` is exempt from the wire scrub; ``error_code`` set means
    empty data. ``offset == size`` answers ``eof`` with no data.
    """

    type: Literal["artifact_chunk"] = "artifact_chunk"
    seq: int = 1
    session_id: str
    artifact_id: str
    offset: int = Field(ge=0)
    size: int = Field(default=0, ge=0)
    sha256: str = ""
    data_b64: str = ""
    eof: bool = False
    error_code: str | None = None


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
    # TD-5015: the paired computer an error came from, or is about. A store
    # that consumes unscoped errors ignores one that carries this.
    machine: str | None = None
    # TD-5144: the client message type this error answers, when the sender
    # knows it (a hub refusing a forwarded verb). Lets a download claim only
    # its own refusals. Absent from older daemons.
    in_reply_to: str | None = None
    # TD-5158: the steering proposal a refused accept, edit or reject named,
    # so the window shows the refusal on that card even after the user has
    # acted on another one. Absent from older daemons.
    proposal_id: str | None = None


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


class FocusWindow(DaemonEvent):
    """Bring the local window forward after a computer-use turn (TD-4832).

    Emitted live when a ``cu_session`` closes and no other session still
    has an open episode — never between tool calls, and never written to
    a session log, so attach replay cannot re-trigger it. Connection-
    scoped; a remote (browser) viewer receives it and no-ops, because
    only the Tauri host can raise the window. Window activation failures
    are swallowed client-side: they must not fail the turn.
    """

    type: Literal["focus_window"] = "focus_window"
    seq: int = 1
    reason: str = "cu_session_closed"


class DesignHitBox(BaseModel):
    """Computed box of a Design-mode hit, in frame CSS pixels."""

    x: float
    y: float
    width: float
    height: float


class DesignHit(DaemonEvent):
    """Reply to ``design_hit_test`` (TD-3403 / TD-3406). Connection-scoped.

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
    # TD-4823: the host's own diagnosis. Defaults keep Windows / Linux /
    # mock payloads unchanged. ``stale_*`` means System Settings shows
    # TST Desk ON but the row belongs to an older build; only a reset
    # repairs it. ``fix_*`` is the host's per-service repair copy.
    actuation_path: Literal["host", "daemon", "mock", "none", ""] = ""
    signing: Literal["identity", "adhoc", "unsigned", ""] = ""
    bundle_path: str = ""
    stale_screen_recording: bool = False
    stale_accessibility: bool = False
    unbundled_dev_binary: bool = False
    fix_screen_recording: str = ""
    fix_accessibility: str = ""
    reset_supported: bool = False
    reset_error: str = ""


class JobEntry(BaseModel):
    """One persisted job on ``job_list`` (TD-3805).

    The ``last_*`` fields are the run receipt (TD-3807). Without them the
    rail can say when a job will next fire but not whether it has ever
    worked, which is the question a user actually has.
    """

    id: str
    workspace: str
    instruction: str
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy", "email"]
    paused: bool = False
    timezone: str | None = None
    # None means the run uses the window's current preset and engine.
    preset: str | None = None
    engine: Literal["native", "grok"] | None = None
    # Seconds a slot may be late and still run. None fires a missed slot
    # once, however old it is (TD-3813). The phrase is not on this event.
    grace: int | None = None
    # Extra tries after the first scheduled fire, and the gap in seconds
    # (TD-3814). ``retry_delay`` is null when retries is 0. ``attempt`` is
    # how many tries this slot has already used. Above 0 the row is waiting
    # on a retry, and a window notification waits for the slot to finish.
    retries: int = 0
    retry_delay: int | None = None
    attempt: int = 0
    # Seconds this job's turn may run. None uses scheduler.max_run_seconds
    # (TD-3819). The phrase is not on this event.
    max_run: int | None = None
    # Another job's id, started once after this one ends ok (TD-3817).
    # None means this job stands alone. The child's schedule is separate.
    then: str | None = None
    # Local .ics that can block a regular slot, and the match list
    # (TD-3818). None means the slot is not blocked by a calendar.
    skip_calendar: str | None = None
    skip_match: str | None = None
    last_run: str | None = None
    # ``waiting`` is a run parked on an approval card (TD-3815). It is
    # not a failure and it is not retried. The same receipt becomes
    # ``ok`` or ``failed`` when that session finishes.
    # ``skipped`` is a regular slot blocked by a local calendar (TD-3818).
    # It is not ``missed`` (that one was late) and nothing is delivered.
    last_status: Literal["ok", "failed", "missed", "waiting", "skipped"] | None = None
    last_summary: str | None = None
    last_session_id: str | None = None
    # One address when ``deliver_to`` is email (TD-3820).
    email_to: str | None = None
    # Mail outcome of the last fire. Null unless that fire was email.
    # Separate from ``last_status``.
    last_delivery: Literal["ok", "failed"] | None = None
    last_delivery_error: str | None = None
    # Not persisted. It mirrors the daemon's in-flight set, so a restart
    # cannot show a turn that is no longer happening (TD-3809).
    running: bool = False


class JobList(DaemonEvent):
    """Response to ``list_jobs`` / ``save_job`` / ``delete_job`` / ``run_job`` (TD-3805).

    Connection-scoped. Seq is fixed at 1 so it cannot rewind attach.
    """

    type: Literal["job_list"] = "job_list"
    seq: int = 1
    jobs: list[JobEntry] = Field(default_factory=list)


class JobRunEntry(BaseModel):
    """One fire on ``job_runs`` (TD-3811).

    ``scheduled_for`` is the slot that fired, or null for Run now.
    ``summary`` is already redacted and capped the same way as
    ``JobEntry.last_summary``. ``missed`` is a slot skipped for lateness,
    not a turn that failed (TD-3813). ``waiting`` is a run parked on an
    approval card; the same line is updated when that turn finishes
    (TD-3815). ``skipped`` is a regular slot blocked by a local calendar
    (TD-3818). Its summary is ``calendar``, not the event title.
    """

    started_at: str
    scheduled_for: str | None = None
    # ``chained`` is a fire started because another job ended ok (TD-3817).
    # ``scheduled_for`` is null, the same as Run now: this was not a slot.
    trigger: Literal["schedule", "manual", "chained"]
    status: Literal["ok", "failed", "missed", "waiting", "skipped"]
    summary: str | None = None
    session_id: str | None = None
    # 1-based try and the budget (retries + 1) when the job retries
    # (TD-3814). Null on a job that does not retry, and on Run now.
    attempt: int | None = None
    attempts: int | None = None
    # Names the job whose ok run started a chained fire (TD-3817). Null
    # on a scheduled slot and on Run now.
    note: str | None = None
    # Mail outcome for this fire (TD-3820). Null unless the channel is email.
    delivery: Literal["ok", "failed"] | None = None
    delivery_error: str | None = None


class JobRuns(DaemonEvent):
    """Response to ``list_job_runs`` (TD-3811).

    Connection-scoped. Seq is fixed at 1 so it cannot rewind attach.
    ``runs`` is newest first.
    """

    type: Literal["job_runs"] = "job_runs"
    seq: int = 1
    job_id: str
    runs: list[JobRunEntry] = Field(default_factory=list)


class JobDraftReply(DaemonEvent):
    """Response to ``parse_job`` (TD-3803). Connection-scoped. Not saved."""

    type: Literal["job_draft"] = "job_draft"
    seq: int = 1
    ok: bool = True
    detail: str = ""
    workspace: str | None = None
    instruction: str | None = None
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy", "email"] | None = None
    email_to: str | None = None
    paused: bool = False


class JobTemplateEntry(BaseModel):
    """One template on ``job_templates`` (TD-3816).

    ``cadence`` is the phrase, not cron. ``next_run`` is an ISO instant or
    the rule ``tomorrow at H:MM``. ``builtin`` rows are not in the file.
    """

    id: str
    name: str
    builtin: bool = False
    instruction: str = ""
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: Literal["window", "slack", "ntfy", "email"]
    grace: int | None = None
    retries: int = 0
    retry_delay: int | None = None
    # Seconds, or null to use scheduler.max_run_seconds (TD-3819).
    max_run: int | None = None
    preset: str | None = None
    engine: Literal["native", "grok"] | None = None
    workspace: str | None = None
    email_to: str | None = None


class JobTemplates(DaemonEvent):
    """Response to list / save / delete template (TD-3816).

    Connection-scoped. Seq is fixed at 1 so it cannot rewind attach.
    """

    type: Literal["job_templates"] = "job_templates"
    seq: int = 1
    templates: list[JobTemplateEntry] = Field(default_factory=list)


class SessionJobSource(DaemonEvent):
    """Response to ``get_session_job_source`` (TD-3816).

    Connection-scoped. Seq is fixed at 1. ``instruction`` is the earliest
    remaining user message, and may be empty. This event does not save a job.
    """

    type: Literal["session_job_source"] = "session_job_source"
    seq: int = 1
    session_id: str
    workspace: str
    preset: str | None = None
    engine: Literal["native", "grok"] | None = None
    instruction: str = ""


class Transcript(DaemonEvent):
    """Reply to ``transcribe`` (TD-4701). Connection-scoped.

    ``detail`` is a short error code on failure (never a URL). ``text`` is
    the transcript on success. Seq is fixed at 1 so it cannot rewind attach.
    """

    type: Literal["transcript"] = "transcript"
    seq: int = 1
    ok: bool = True
    text: str = ""
    detail: str = ""
    error: str | None = None


class EmailTestResult(DaemonEvent):
    """Reply to ``test_email`` (TD-3820). Connection-scoped.

    ``error_class`` is the SMTP exception name. ``message`` is the public
    text, already stripped of the password and the message body.
    """

    type: Literal["email_test_result"] = "email_test_result"
    seq: int = 1
    ok: bool = False
    error_class: str = ""
    message: str = ""


class GithubStatus(DaemonEvent):
    """GitHub connector snapshot (TD-4852). Connection-scoped.

    Seq is fixed at 1 so it cannot rewind a session attach. The same
    shape rides on ``setup_state.github`` for a window that opens later.
    """

    type: Literal["github_status"] = "github_status"
    seq: int = 1
    enabled: bool = False
    connected: bool = False
    phase: GithubPhase = "off"
    user_code: str = ""
    verification_uri: str = ""
    login: str | None = None
    message: str = ""


class MicrosoftStatus(DaemonEvent):
    """Microsoft connector snapshot (TD-4853). Connection-scoped.

    Seq is fixed at 1 so it cannot rewind a session attach. The same
    shape rides on ``setup_state.microsoft``. ``account`` is the mailbox,
    not a token.
    """

    type: Literal["microsoft_status"] = "microsoft_status"
    seq: int = 1
    enabled: bool = False
    connected: bool = False
    phase: MicrosoftPhase = "off"
    account: str | None = None
    message: str = ""


class GoogleStatus(DaemonEvent):
    """Google connector snapshot (TD-4854). Connection-scoped.

    Seq is fixed at 1 so it cannot rewind a session attach. The same
    shape rides on ``setup_state.google``. ``account`` is the mailbox.
    ``client_secret_stored`` is a presence probe. The secret, the tokens,
    and the authorization code are not included.
    """

    type: Literal["google_status"] = "google_status"
    seq: int = 1
    enabled: bool = False
    connected: bool = False
    phase: GooglePhase = "off"
    account: str | None = None
    message: str = ""
    client_secret_stored: bool = False


# ── Discriminated unions ───────────────────────────────────────────────

ClientMessageT = Annotated[
    Hello
    | Pair
    | OpenWorkspace
    | UserMessage
    | SendQueued
    | RemoveQueued
    | Approve
    | Deny
    | AlwaysAllow
    | ListPolicyRules
    | RevokePolicyRule
    | ForkFrom
    | CompactContext
    | SetBranch
    | SetSkipAllApprovals
    | SetLoadGlobalMemory
    | SetCoworker
    | SetVoice
    | SetCuIndicators
    | SetCuPolicy
    | SetJudgments
    | SetWorkspacePin
    | Resume
    | Cancel
    | StopTurn
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
    | SteeringAccept
    | SteeringEdit
    | SteeringReject
    | EndSession
    | Shutdown
    | Drain
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
    | SetEngine
    | SetGrokMode
    | RunGrokCommand
    | ListGrokSessions
    | OpenInTerminal
    | ApproveGrokPlan
    | ListGrokExtensions
    | SetSpendCap
    | TrustWorkspaceConfig
    | SetTierSlug
    | SetCredential
    | DeleteCredential
    | SetTierCredential
    | SetMcpServer
    | DeleteMcpServer
    | SetMcpSecret
    | DeleteMcpSecret
    | RunDiagnostics
    | GetUsage
    | ExportUsage
    | DeleteApiKey
    | ListArtifacts
    | OpenArtifact
    | ReadArtifactChunk
    | DesignHitTest
    | CheckCuPermissions
    | ResetCuPermissions
    | SetCuKill
    | SetRemoteAttach
    | RotateRemoteToken
    | StartPairing
    | CancelPairing
    | RemoveRemoteDevice
    | AddMachine
    | RemoveMachine
    | ListMachines
    | ReconnectMachine
    | ListJobs
    | SaveJob
    | DeleteJob
    | ParseJob
    | RunJob
    | ListJobRuns
    | ListJobTemplates
    | SaveJobTemplate
    | DeleteJobTemplate
    | GetSessionJobSource
    | Transcribe
    | SetEmailNotify
    | TestEmail
    | GithubConnect
    | GithubDisconnect
    | MicrosoftConnect
    | MicrosoftDisconnect
    | GoogleConnect
    | GoogleDisconnect,
    Field(discriminator="type"),
]

DaemonEventT = Annotated[
    Ready
    | SessionState
    | ConversationReset
    | UserTurn
    | QueuedMessages
    | AssistantDelta
    | AssistantReasoning
    | AssistantRetract
    | ToolCall
    | ToolResult
    | ShellOutput
    | ToolProgress
    | ApprovalRequest
    | DecisionLogged
    | CheckpointNotice
    | VerifyStarted
    | VerifyResult
    | CostUpdate
    | BoundaryUpdate
    | TurnComplete
    | TierState
    | ContextCompacted
    | ContextUsage
    | TaskList
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
    | SteeringProposal
    | SteeringDecision
    | SessionList
    | PolicyRules
    | SetupState
    | PairingCode
    | PairingCompleted
    | MachineList
    | DrainResult
    | ApiKeyValidated
    | DiagnosticsReport
    | UsageReport
    | UsageExported
    | LogTrimmed
    | ArtifactReady
    | ArtifactList
    | Artifact
    | ArtifactChunk
    | Ping
    | Error
    | ScreenFrame
    | CuKillState
    | CuSession
    | FocusWindow
    | DesignHit
    | CuPermissions
    | JobList
    | JobRuns
    | JobDraftReply
    | JobTemplates
    | SessionJobSource
    | EmailTestResult
    | GithubStatus
    | MicrosoftStatus
    | GoogleStatus
    | GrokCommands
    | GrokPlan
    | GrokMode
    | GrokPreview
    | GrokSessionList
    | GrokExtensions
    | Transcript,
    Field(discriminator="type"),
]

_client_message_adapter: TypeAdapter[ClientMessageT] = TypeAdapter(ClientMessageT)
_daemon_event_adapter: TypeAdapter[DaemonEventT] = TypeAdapter(DaemonEventT)

# Known message types for explicit unknown-type detection
_KNOWN_CLIENT_TYPES = frozenset(
    {
        "hello",
        "pair",
        "open_workspace",
        "user_message",
        "send_queued",
        "remove_queued",
        "approve",
        "deny",
        "always_allow",
        "list_policy_rules",
        "revoke_policy_rule",
        "fork_from",
        "compact_context",
        "set_branch",
        "set_skip_all_approvals",
        "set_load_global_memory",
        "set_judgments",
        "set_coworker",
        "set_voice",
        "set_cu_indicators",
        "set_cu_policy",
        "set_workspace_pin",
        "resume",
        "cancel",
        "stop_turn",
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
        "steering_accept",
        "steering_edit",
        "steering_reject",
        "end_session",
        "shutdown",
        "drain",
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
        "set_engine",
        "set_grok_mode",
        "run_grok_command",
        "list_grok_sessions",
        "open_in_terminal",
        "approve_grok_plan",
        "list_grok_extensions",
        "set_spend_cap",
        "trust_workspace_config",
        "set_tier_slug",
        "set_credential",
        "delete_credential",
        "set_tier_credential",
        "set_mcp_server",
        "delete_mcp_server",
        "set_mcp_secret",
        "delete_mcp_secret",
        "run_diagnostics",
        "get_usage",
        "export_usage",
        "list_artifacts",
        "open_artifact",
        "read_artifact_chunk",
        "design_hit_test",
        "check_cu_permissions",
        "reset_cu_permissions",
        "set_cu_kill",
        "set_remote_attach",
        "rotate_remote_token",
        "start_pairing",
        "cancel_pairing",
        "remove_remote_device",
        "add_machine",
        "remove_machine",
        "list_machines",
        "reconnect_machine",
        "list_jobs",
        "save_job",
        "delete_job",
        "parse_job",
        "run_job",
        "list_job_runs",
        "list_job_templates",
        "save_job_template",
        "delete_job_template",
        "get_session_job_source",
        "transcribe",
        "set_email_notify",
        "test_email",
        "github_connect",
        "github_disconnect",
        "microsoft_connect",
        "microsoft_disconnect",
        "google_connect",
        "google_disconnect",
    }
)
_KNOWN_EVENT_TYPES = frozenset(
    {
        "ready",
        "session_state",
        "conversation_reset",
        "user_turn",
        "queued_messages",
        "assistant_delta",
        "assistant_reasoning",
        "assistant_retract",
        "tool_call",
        "tool_result",
        "shell_output",
        "tool_progress",
        "approval_request",
        "decision_logged",
        "checkpoint_notice",
        "verify_started",
        "verify_result",
        "cost_update",
        "boundary_update",
        "turn_complete",
        "tier_state",
        "context_compacted",
        "context_usage",
        "task_list",
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
        "steering_proposal",
        "steering_decision",
        "session_list",
        "policy_rules",
        "setup_state",
        "pairing_code",
        "pairing_completed",
        "machine_list",
        "drain_result",
        "api_key_validated",
        "diagnostics_report",
        "usage_report",
        "usage_exported",
        "log_trimmed",
        "artifact_ready",
        "artifact_list",
        "artifact",
        "artifact_chunk",
        "ping",
        "error",
        "screen_frame",
        "cu_kill_state",
        "cu_session",
        "focus_window",
        "design_hit",
        "cu_permissions",
        "job_list",
        "job_runs",
        "job_draft",
        "job_templates",
        "session_job_source",
        "grok_commands",
        "grok_plan",
        "grok_mode",
        "grok_preview",
        "grok_session_list",
        "grok_extensions",
        "transcript",
        "email_test_result",
        "github_status",
        "microsoft_status",
        "google_status",
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


#: Messages whose input may hold an MCP server's token (TD-5110).
_MCP_VALUE_VERBS = frozenset(
    {"set_mcp_server", "delete_mcp_server", "set_mcp_secret", "delete_mcp_secret"}
)


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
        if data["type"] == "add_machine":
            # A validation message quotes its input, so it would echo a code.
            raise HandshakeError(
                "bad_request", "Malformed add_machine: give a link, or host, port and code."
            ) from None
        if data["type"] == "set_email_notify":
            # Same reason: the input may be the SMTP password (TD-3820).
            raise HandshakeError(
                "bad_request", "Malformed set_email_notify: check the email settings fields."
            ) from None
        if data["type"] in _MCP_VALUE_VERBS and isinstance(e, ValidationError):
            # The reasons without the input, which may be a token (TD-5110).
            reasons = "; ".join(
                f"{'.'.join(str(part) for part in err['loc'][1:]) or 'message'}: {err['msg']}"
                for err in e.errors(include_url=False, include_input=False, include_context=False)
            )
            raise HandshakeError("bad_request", f"Malformed {data['type']}: {reasons}") from None
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


_MALFORMED_HELLO = "Malformed hello message"


def parse_hello(raw: str) -> Hello:
    """Parse a raw JSON string into a Hello message (for the handshake).

    The error is fixed text: a validation message echoes its input, and
    the handshake rejection is logged and sent back, so it would carry
    the token. An unknown ``type`` stays ``unknown_message``.
    """
    try:
        msg = parse_client_message(raw)
    except UnknownMessageTypeError:
        raise
    except HandshakeError:
        raise HandshakeError("bad_request", _MALFORMED_HELLO) from None
    if not isinstance(msg, Hello):
        raise HandshakeError("bad_request", _MALFORMED_HELLO)
    return msg


def build_hello_ack(machine_id: str | None = None, desktop_name: str | None = None) -> str:
    """Build the server's handshake acknowledgement.

    ``machine_id`` and ``desktop_name`` (TD-5015) say which computer this
    is, so a hub can tell "same computer at a new address" and "this is
    me". Sent only after the token checked out.
    """
    payload: dict[str, Any] = {"type": "hello_ack", "version": PROTOCOL_VERSION}
    if machine_id is not None:
        payload["machine_id"] = machine_id
    if desktop_name is not None:
        payload["desktop_name"] = desktop_name
    return json.dumps(payload)


def handshake_type(raw: str) -> str | None:
    """The ``type`` of a first frame, or None when it has none.

    Only picks the path (``pair`` or ``hello``); each path parses and
    rejects the frame itself.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    return kind if isinstance(kind, str) else None


def parse_pair(raw: str) -> Pair:
    """Parse a first-frame ``pair`` (TD-5014). Anything else is bad_request.

    The error never quotes the frame: a validation message echoes its
    input, and the handshake rejection is logged, so it would log the code.
    """
    try:
        msg = parse_client_message(raw)
    except HandshakeError:
        raise HandshakeError(
            "bad_request", "Malformed pair message: it needs code, device_name and version."
        ) from None
    if not isinstance(msg, Pair):
        raise HandshakeError("bad_request", "Expected a pair message")
    return msg


def build_pair_ok(
    device_id: str,
    device_name: str,
    token: str,
    desktop_name: str,
    machine_id: str | None = None,
) -> str:
    """The redeem reply (TD-5014). Out-of-band like ``hello_ack``: no seq.

    Carries the device's key, so it goes straight to the socket — never
    through a log or the event funnel. ``machine_id`` (TD-5015) is this
    computer's identity, for a hub pairing it.
    """
    payload: dict[str, Any] = {
        "type": "pair_ok",
        "version": PROTOCOL_VERSION,
        "device_id": device_id,
        "device_name": device_name,
        "token": token,
        "desktop_name": desktop_name,
    }
    if machine_id is not None:
        payload["machine_id"] = machine_id
    return json.dumps(payload)


def build_ping() -> str:
    """Build the application-level liveness frame (TD-1716)."""
    return Ping().model_dump_json()


def build_error(
    code: str,
    message: str,
    session_id: str | None = None,
    machine: str | None = None,
    in_reply_to: str | None = None,
    proposal_id: str | None = None,
) -> str:
    """Build a typed error message.

    The message passes through the shared redaction chokepoint (TD-1405):
    this envelope bypasses the event log — it is written straight to the
    socket — so it must scrub here rather than rely on ``event_log.add``.
    ``session_id`` (optional, TD-1711) lets the UI attribute the error to
    the session whose message was refused. ``machine`` (TD-5015) marks an
    error that came from, or is about, a paired computer. ``in_reply_to``
    (TD-5144) names the client message type the error answers, and
    ``proposal_id`` (TD-5158) the steering proposal it was about.
    """
    payload: dict[str, str] = {"type": "error", "code": code, "message": redact_secrets(message)}
    if session_id is not None:
        payload["session_id"] = session_id
    if machine is not None:
        payload["machine"] = machine
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to
    if proposal_id is not None:
        payload["proposal_id"] = proposal_id
    return json.dumps(payload)


# Fields that carry file bytes as base64, by message type (TD-5143). The rest of
# the frame is still scrubbed; a value that is not pure base64 is scrubbed as usual.
_WIRE_SCRUB_EXEMPT: dict[str, str] = {"artifact_chunk": "data_b64"}
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def scrub_wire_json(payload: str) -> str:
    """Redact secret-shaped strings in a serialized daemon→client frame.

    The daemon's reply funnel runs every direct reply through here
    (TD-4802): connection-scoped replies (``session_list``,
    ``memory_files``, ``setup_state``, …) bypass the session event log,
    where redaction normally happens at insertion, and their payloads can
    be user text — a session title is the first user message.  Idempotent:
    a frame already scrubbed at insertion passes through unchanged.

    One targeted exemption (TD-5143): ``artifact_chunk.data_b64`` is file
    bytes. Base64 can spell ``AKIA…`` by chance, and scrubbing it would
    corrupt the chunk on every retry, so the file could never verify. The
    field is held out only while it is pure base64.
    """
    obj = json.loads(payload)
    held: tuple[str, str] | None = None
    if isinstance(obj, dict):
        field = _WIRE_SCRUB_EXEMPT.get(str(obj.get("type")))
        value = obj.get(field) if field else None
        if field and isinstance(value, str) and _BASE64.fullmatch(value):
            held = (field, obj.pop(field))
    out = redact_structure(obj)
    if held is not None and isinstance(out, dict):
        out[held[0]] = held[1]
    return json.dumps(out, ensure_ascii=False)


def validate_hello(hello: Hello) -> None:
    """Validate the version and token of a parsed hello message."""
    validate_version(hello.version)
