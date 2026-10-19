"""Session model, event log, and registry.

The session owns the agent loop. The socket is only a viewer.
The daemon owns sessions; the window is only a viewer (prime directive §2.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from .autonomy.charter import Charter
from .autonomy.classifier import DecisionClass
from .autonomy.judgment import JudgmentBackend
from .boundary_config import BoundaryConfig
from .compaction import ContextMeter, compact_on_request, window_user_turns
from .logging import get_logger, redact_structure
from .policy import (
    ApprovalOutcome,
    PolicyConfig,
    propose_always_allow,
    rule_summary,
    stricter_rule,
)
from .protocol import (
    ConversationReset,
    DaemonEvent,
    ToolResult,
)
from .protocol import SessionState as SessionStateEvent
from .provider import ChatMessage
from .workspace_trust import TrustStore, WorkspaceConfigView

if TYPE_CHECKING:
    from .config import ModelConfig
    from .context.memory_loader import MemoryLoad
    from .cost import CostTracker
    from .router import TierRouter
    from .steering_proposals import ParkedSteering
    from .tools.registry import Tool

log = get_logger("tstd.session")


@dataclass(frozen=True)
class QueuedUserMessage:
    """A user turn waiting for the loop (TD-4705).

    ``display`` feeds the event log, memory loader, and assembler;
    ``provider`` is what the OpenAI-compatible client sends (plain text
    or multimodal parts when images are attached).
    """

    display: str
    provider: str | list[dict[str, Any]]
    # TD-5155: the id the message is stored under until its turn starts.
    # The loop logs it as the user_turn's turn_id, so a restart can tell a
    # message whose turn began from one that was still waiting.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Images the queue file could not keep (TD-5155); 0 for a live send.
    images_dropped: int = 0

    @classmethod
    def plain(cls, text: str) -> QueuedUserMessage:
        return cls(display=text, provider=text)

    @property
    def prompt_text(self) -> str:
        """The text an engine that is sent ``display`` gets instead (TD-5155).

        ``display`` names images a restart did not keep as attached; the
        held message's ``provider`` text says they were dropped.
        """
        if self.images_dropped and isinstance(self.provider, str):
            return self.provider
        return self.display


class SessionError(Exception):
    """Session-related error."""


@dataclass
class PendingApproval:
    """A parked approval and the metadata needed to act on it (TD-802/803).

    Carries the call's tool, arguments, and decision class so the daemon can
    generate the "always allow" rule without re-deriving them (TD-803).
    ``tool`` is ``None`` for external-import approvals (TD-505), which are
    class-C and can never be always-allowed.
    """

    future: asyncio.Future[tuple[bool, str | None]]
    tool: Tool | None
    arguments: dict[str, Any]
    decision_class: DecisionClass


def _redact_event(event: DaemonEvent) -> DaemonEvent:
    """Scrub secret-shaped text before an event is stored or broadcast (TD-1405).

    The event log is the single pipeline feeding client replay, the
    WebSocket broadcast, and the audit writer — redacting at insertion
    covers all three at once.  The model's conversation is built from
    dispatch results, not logged events, so execution is unaffected, and
    the caller's own event object is left untouched (a copy is stored).

    The scrub is whole-event (TD-4802): every string field of every event
    type passes through ``redact_structure``.  The per-type field list this
    replaced let ``ApprovalRequest.summary``, ``DecisionLogged.what/why``,
    and the assistant deltas through raw — each added after TD-1405 by
    authors who reasonably assumed the chokepoint was categorical.
    Shape-constrained fields (Literals, enums, generated ids) never match
    the patterns; a field that does match is carrying a secret and must be
    scrubbed wherever it sits.

    The union's contract is path-not-bytes (``ScreenFrame``, ``Artifact``):
    no event carries bulk payload text such as a data URL, where a regex
    hit would corrupt the payload.  A future event that carries one needs
    a targeted exemption here, not a bypass of the scrub.
    """
    dumped = event.model_dump()
    scrubbed = redact_structure(dumped)
    if scrubbed == dumped:
        return event
    return type(event).model_validate(scrubbed)


# ── Event log ──────────────────────────────────────────────────────────


class EventSubscriber(Protocol):
    """Protocol for event subscribers."""

    async def __call__(self, event: DaemonEvent, log: SessionEventLog) -> None: ...


class SessionEventLog:
    """Monotonic event log with a seq per session.

    Every event is stamped with the next seq number on insertion.
    Seq numbers never rewind. The durable window may drop a prefix
    (``drop_before``) so memory matches the on-disk cap; ``last_seq``
    stays the highest seq ever assigned.

    Subscribers are notified of every new event after it is appended.
    A subscriber is a callable that receives the event and the log.
    """

    def __init__(self) -> None:
        self._events: list[DaemonEvent] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._new_event = asyncio.Event()
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, callback: EventSubscriber) -> None:
        """Register a callback to be notified of new events."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: EventSubscriber) -> None:
        """Remove a previously registered callback."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    async def add(self, event: DaemonEvent) -> DaemonEvent:
        """Append an event, stamping it with the next monotonic seq.

        Args:
            event: The event to append. Its seq field is overwritten.

        Returns:
            The event with its seq set, for convenience.
        """
        event = _redact_event(event)
        async with self._lock:
            self._seq += 1
            # Pydantic v2 allows attribute assignment on non-frozen models
            event.seq = self._seq
            self._events.append(event)
            self._new_event.set()
            self._new_event = asyncio.Event()

        # Notify subscribers outside the lock so they can read the log
        for sub in self._subscribers:
            try:
                await sub(event, self)
            except Exception:
                log.exception("event subscriber failed")
        return event

    def events_from(self, seq: int) -> list[DaemonEvent]:
        """Return all events with seq >= `seq`."""
        if seq < 1:
            seq = 1
        return sorted((e for e in self._events if e.seq >= seq), key=lambda event: event.seq)

    async def wait_for_new_event(self, seen_seq: int) -> int:
        """Wait until a new event beyond `seen_seq` is available.

        Returns the new last_seq.
        """
        if self._seq > seen_seq:
            return self._seq
        await self._new_event.wait()
        return self._seq

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def earliest_seq(self) -> int:
        """Smallest seq still in the window. ``1`` when the log is empty."""
        return self._events[0].seq if self._events else 1

    @property
    def all_events(self) -> list[DaemonEvent]:
        return list(self._events)

    async def drop_before(self, earliest_seq: int) -> None:
        """Drop events older than the on-disk window. Does not rewind last_seq."""
        async with self._lock:
            self._events = [event for event in self._events if event.seq >= earliest_seq]

    def replace(self, events: list[DaemonEvent]) -> None:
        """Install a persisted log. Does not notify subscribers or re-seq.

        Used on revive. Re-adding would mint new seqs and rewrite disk.
        """
        ordered = sorted(events, key=lambda event: event.seq)
        self._events = ordered
        self._seq = max(event.seq for event in ordered) if ordered else 0


# ── Session ────────────────────────────────────────────────────────────


class Session:
    """A session owns the agent loop and its event log.

    State machine:
        idle → running → awaiting_approval → running → complete / failed / cancelled
        running → paused (cap fault) → running (resume)
    """

    # Valid state transitions: current_state → {allowed_next_states}
    VALID_TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "idle": {"running"},
        "running": {"awaiting_approval", "paused", "complete", "failed", "cancelled"},
        "awaiting_approval": {"running", "cancelled"},
        "paused": {"running", "cancelled"},
        "complete": set(),
        "failed": set(),
        "cancelled": set(),
        # `interrupted` is a tombstone only when nothing durable survived.
        # A session with a persisted log and conversation is revived as
        # idle and started again — not marked interrupted and left dead.
        "interrupted": set(),
    }

    def __init__(self, workspace_path: str) -> None:
        self.id = str(uuid.uuid4())
        self.workspace_path = workspace_path
        # Session persist dir (TD-1710): screenshots land here, not in the workspace.
        self.persist_dir: Path | None = None
        self._state = "idle"
        self.event_log = SessionEventLog()
        self._cancel_event = asyncio.Event()
        # Stop the in-flight turn without ending the session (TD-5003).
        self._stop_turn_event = asyncio.Event()
        self._user_message_queue: asyncio.Queue[QueuedUserMessage] = asyncio.Queue()
        # TD-5155: what the daemon accepted and no turn has logged yet
        # (``_waiting``, in queue order), and what a restart brought back
        # for the person to send or remove (``held_messages``). Both reach
        # disk through ``_queue_hook`` before a send returns.
        self._waiting: list[QueuedUserMessage] = []
        self.held_messages: list[QueuedUserMessage] = []
        self._queue_hook: Callable[[list[QueuedUserMessage]], Awaitable[None]] | None = None
        self._queue_lock = asyncio.Lock()
        # The loop's conversation lives here so a fork can truncate it
        # (TD-1708).  The loop aliases this list; it must not rebind.
        self.conversation: list[ChatMessage] = []
        # TD-5109: how the loop last measured the prompt, so a compaction
        # asked for between turns counts tokens as the next request will.
        self.context_meter: ContextMeter | None = None
        # user_index -> sibling snapshots of conversation
        self._branches: dict[int, list[list[ChatMessage]]] = {}
        self._branch_cursor: dict[int, int] = {}
        self._resume_event = asyncio.Event()
        # Workspace boundary (TD-706), resolved by the daemon on open.
        self.boundary_config = BoundaryConfig()
        # Tier router (TD-1006 / TD-4603), attached by the daemon on
        # open so ``set_tier`` and ``set_plan`` reach the live loop.
        # Plan mode lives on the router. ``None`` on a restored
        # tombstone — its loop is gone for good. Revive does not
        # restore override or plan (neither is persisted).
        self.router: TierRouter | None = None
        # Cost tracker (TD-1201), attached by the loop at startup so the
        # daemon can answer cache-state queries. Same tombstone rule.
        self.cost_tracker: CostTracker | None = None
        # Approval policy (TD-801), resolved by the daemon on open.
        self.policy = PolicyConfig()
        # Workspace config trust (TD-5113). The daemon attaches its store
        # before the first config load; None (library use) gates nothing.
        # ``config_view`` is the read the boundary was last loaded from.
        self.trust_store: TrustStore | None = None
        self.config_view: WorkspaceConfigView | None = None
        # Pending approvals (TD-802), owned by the session — NOT by any
        # websocket — so a client disconnect leaves them parked and
        # resumable (prime directive §2.5).  Each record carries the call
        # metadata (TD-803) so "always allow" can generate its rule.
        self._pending_approvals: dict[str, PendingApproval] = {}
        # Files the session has touched via successful path-bearing tool
        # calls (TD-503), as workspace-relative posix strings.  The loop
        # passes this set to the assembler so path-scoped steering rules
        # activate only once a matching file is in play.
        self.touched_paths: set[str] = set()
        self._workspace_root: Path | None = None  # resolved lazily by record_touched
        # Set by the loop (TD-1715): rebinds what it built from the old root
        # (dispatcher, assembler, import approvals) after a live move.
        self.on_workspace_moved: Callable[[], None] | None = None
        # Set by the loop (TD-5113): re-applies a reloaded boundary and
        # policy (path guard, classifier wall, policy gate, import
        # approvals) without touching root-bound pieces, so it is safe
        # while a turn runs.
        self.on_config_reloaded: Callable[[], None] | None = None
        # Turns owed to the user (TD-1715): raised when a message is queued,
        # lowered when the loop reports that turn finished. `session_state`
        # cannot answer "is a turn in flight" — "running" means the loop is
        # alive and spans the session's whole life (TD-1714) — so the count
        # is tracked here, off the one event that proves a turn ended.
        self._open_turns = 0
        # After-write reviews running now (TD-5151). A review starts after
        # ``turn_complete``, when no turn is open, so Stop counts these too.
        self._open_reviews = 0
        self.event_log.subscribe(self._observe_turn_end)  # type: ignore[arg-type]
        # Set by the daemon when a persist backend is attached. The loop
        # calls conversation_changed after it mutates ``conversation``.
        self._conversation_hook: Callable[[], Awaitable[None]] | None = None
        # Last brain-turn memory selection (TD-2604). None until a brain
        # turn has run the loader.
        self.last_memory: MemoryLoad | None = None
        # TD-4502: skill names loaded this session via load_skill or slash.
        self.loaded_skills: list[str] = []
        # TD-5140: steering edits the agent proposed, by proposal id, until
        # the user decides on the card. In memory only; nothing accepts one
        # but a user message (not skip-all, not an autonomy run).
        self.steering_proposals: dict[str, ParkedSteering] = {}
        # TD-4602: 0 = user-visible parent; 1 = transient worker child.
        # Children are not registered, persisted, or revived.
        self.delegate_depth: int = 0
        self.parent_id: str | None = None
        # Bound by agent_loop so ``delegate`` can spawn a worker child.
        self.delegate_runtime: Any = None
        # Catalog preset this loop is using (TD-1721). Name on the
        # session record; ``config`` is a snapshot of that catalog
        # entry, not a fork written back to Settings.
        self.preset: str = ""
        self.config: ModelConfig | None = None
        # TD-2603: machine-wide opt-in. The daemon stamps this on open
        # and when the Settings toggle flips.
        self.load_global_memory = False
        # TD-3903: this session has used a desktop_ / browser_ tool.
        # Live flag; revive also scans the event log (same signal as Screen).
        self.used_cu = False
        # TD-3407: computer-use episode is open this turn. The overlay
        # callback is the sidecar ring; None on a tombstone or mock-only.
        self.cu_session_active = False
        self._overlay_session: Callable[[bool], Awaitable[None]] | None = None
        # TD-4101: unattended runs iterate without a user message.
        # Interactive sessions leave this false and wait on the queue.
        self.autonomy = False
        self.charter: Charter | None = None
        self.autonomy_turns = 0
        self.autonomy_class_c = False
        self.autonomy_class_b = False
        self.autonomy_stop_reason: str | None = None
        self.autonomy_notify: Callable[[str], Awaitable[None]] | None = None
        # TD-4303: Class C reasons kept after the stop reason is rewritten
        # to CLASS_C_STOP, plus any other stored refusal strings.
        self.autonomy_refusals: list[str] = []
        # TD-4103: attached by the loop on an unattended run. Interactive
        # sessions leave this None so a turn complete never polls DoD.
        self.dod_poller: Callable[[], Awaitable[Any]] | None = None
        # Image bytes for the next Grok ACP prompt (name, bytes, media_type).
        self.pending_images: list[tuple[str, bytes, str]] = []
        self.grok_acp: Any = None
        self.grok_session_id: str | None = None
        # Loop this session started with. New sessions take config.engine.kind;
        # a running session keeps this even if Settings later flips.
        self.engine: Literal["native", "grok"] = "native"
        self.last_dod_poll: Any = None
        # TD-4203: circuit-breaker counters. Interactive sessions never
        # trip; the loop only records when autonomy is on.
        self.red_dod_streak: int = 0
        self.no_dod_progress_streak: int = 0
        self.last_dod_green_count: int | None = None
        self.file_write_counts: dict[str, int] = {}
        self.last_tool_fingerprint: tuple[tuple[str, str], ...] | None = None
        self.tool_loop_streak: int = 0
        self._breaker_seen_turn: int | None = None
        # TD-710 (dev): semantic no-progress streak, plus the session's
        # judgment backend — None unless a judgments feature is enabled,
        # which leaves the semantic breaker inert.
        self.semantic_no_progress_streak: int = 0
        self.judgment_backend: JudgmentBackend | None = None
        # The configured confidence threshold, set when the backend is
        # wired so the breaker uses the user's value, not the default.
        self.judgment_threshold: float = 0.6
        # TD-4201 / TD-4202: validator drift check, last good checkpoint,
        # and consecutive-drift streak. Interactive sessions never set these.
        self.autonomy_check_every = 5
        self.autonomy_last_check_sha: str | None = None
        self.autonomy_last_good_sha: str | None = None
        self.autonomy_drift_streak: int = 0
        self.autonomy_ledger_seen = 0
        self.last_drift_check: Any = None
        self.validator_call: Callable[[str], Awaitable[str]] | None = None

    def mark_class_c(self, reason: str) -> None:
        """A Class C call on an autonomous run — stop after this turn."""
        self.autonomy_class_c = True
        self.autonomy_refusals.append(reason)
        if self.autonomy_stop_reason is None:
            self.autonomy_stop_reason = reason

    async def open_cu_session(self, tool_name: str) -> None:
        """Open the computer-use episode on the first CU tool of a turn."""
        from .local_worker import is_cu_tool
        from .protocol import CuSession

        if self.cu_session_active or not is_cu_tool(tool_name):
            return
        self.cu_session_active = True
        await self.event_log.add(CuSession(session_id=self.id, active=True, seq=1))
        if self._overlay_session is not None:
            await self._overlay_session(True)

    async def close_cu_session(self) -> None:
        """Close the computer-use episode. Idempotent."""
        from .protocol import CuSession

        if not self.cu_session_active:
            return
        self.cu_session_active = False
        await self.event_log.add(CuSession(session_id=self.id, active=False, seq=1))
        if self._overlay_session is not None:
            await self._overlay_session(False)

    async def _observe_turn_end(self, event: DaemonEvent, _log: SessionEventLog) -> None:
        """Lower the open-turn count when the loop reports a turn complete."""
        from .protocol import TurnComplete

        if isinstance(event, TurnComplete):
            self._open_turns = max(0, self._open_turns - 1)

    @property
    def turn_in_flight(self) -> bool:
        """True while the loop owes the user a turn (TD-1715).

        Terminal sessions always read False: their loop is gone, so an
        unanswered message can never become a turn and must not wedge the
        session against Delete forever.
        """
        if not self.VALID_TRANSITIONS.get(self._state, set()):
            return False
        return self._open_turns > 0

    def reassign_workspace(self, workspace_path: str) -> None:
        """Point the session at another workspace (TD-1715 move to project).

        The event log, id, and conversation stay exactly as they are — only
        the working context moves. Touched paths are dropped because they
        are relative to the workspace that no longer applies; keeping them
        would activate path-scoped steering rules against the wrong tree.
        """
        self.workspace_path = workspace_path
        self._workspace_root = None
        self.touched_paths.clear()

    def record_touched(self, paths: Iterable[str]) -> None:
        """Mark tool-call paths as touched (TD-503).

        Relative paths are kept as-is (the tools interpret them against
        the workspace root); absolute paths are relativized against it.
        Paths outside the workspace are dropped — the boundary guard has
        already refused them, and they can never match a scoped rule.
        """
        for raw in paths:
            p = Path(raw)
            if p.is_absolute():
                if self._workspace_root is None:
                    self._workspace_root = Path(self.workspace_path).resolve()
                try:
                    p = p.resolve().relative_to(self._workspace_root)
                except ValueError:
                    continue
            self.touched_paths.add(p.as_posix())

    async def conversation_changed(self) -> None:
        """Snapshot the model conversation after the loop mutates it."""
        if self._conversation_hook is not None:
            await self._conversation_hook()

    @classmethod
    def restore(
        cls,
        session_id: str,
        workspace_path: str,
        state: str = "interrupted",
    ) -> Session:
        """Re-insert a session after a daemon restart (TD-1002).

        ``state`` is whatever the caller decided is honest: ``interrupted``
        when no transcript survived, ``idle`` when a persist snapshot is
        about to be loaded and the loop will start again.
        """
        session = cls(workspace_path)
        session.id = session_id
        session._state = state
        return session

    @property
    def state(self) -> str:
        return self._state

    async def set_state(self, new_state: str, reason: str | None = None) -> None:
        """Transition to a new state and log the transition event.

        Raises:
            SessionError: If the transition is invalid.
        """
        allowed = self.VALID_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise SessionError(
                f"Cannot transition from {self._state!r} to {new_state!r}. "
                f"Allowed: {sorted(allowed) or 'none'}"
            )
        self._state = new_state
        # Create event with seq=1 (overridden by the log)
        event = SessionStateEvent(
            session_id=self.id,
            state=cast(Any, new_state),
            seq=1,
            reason=reason,
            engine=self.engine,
        )
        await self.event_log.add(event)

    async def cancel(self, reason: str = "cancelled by user") -> None:
        """Request cancellation of this session.

        *reason* lands on the ``cancelled`` state event. Daemon shutdown
        passes its own so the log does not blame the user (TD-5018).
        """
        self._cancel_event.set()
        # Nothing will answer the queued turns now.
        self._open_turns = 0
        # Wake any parked approvals so their dispatchers unwind.
        for pending in self._pending_approvals.values():
            pending.future.cancel()
        if self._state in ("running", "awaiting_approval", "paused"):
            await self.set_state("cancelled", reason=reason)
        await self.close_cu_session()
        # Wake a loop parked in wait_for_resume so it observes cancellation.
        self._resume_event.set()

    async def stop_turn(self) -> None:
        """Abort the in-flight turn. The session and its transcript stay.

        ``cancel`` ends the session. This does not. An idle session ignores
        it, so a click that lands as the turn finishes cannot kill the next
        one. A paused turn returns to ``running``; the loop then seals the
        turn instead of continuing it. A running after-write review is not
        idle: Stop ends it (TD-5151).
        """
        if self.cancel_requested:
            return
        if self._state == "running" and not (self.turn_in_flight or self.review_in_flight):
            return
        if self._state not in ("running", "awaiting_approval", "paused"):
            return
        self._stop_turn_event.set()
        for pending in self._pending_approvals.values():
            if not pending.future.done():
                pending.future.set_result((False, "stopped"))
        if self._state == "paused":
            await self.set_state("running", reason="turn stopped")
        # After the state change, so a loop parked on the pause wakes
        # into ``running`` and seals the turn.
        self._resume_event.set()

    def acknowledge_stop_turn(self) -> None:
        """Drop the stop flag once the loop has finished that turn."""
        self._stop_turn_event.clear()

    @property
    def stop_turn_requested(self) -> bool:
        return self._stop_turn_event.is_set()

    async def wait_for_turn_abort(self) -> None:
        """Block until the session is cancelled or this turn is stopped."""
        if self.cancel_requested or self.stop_turn_requested:
            return
        cancel_wait = asyncio.create_task(self._cancel_event.wait())
        stop_wait = asyncio.create_task(self._stop_turn_event.wait())
        try:
            await asyncio.wait(
                {cancel_wait, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (cancel_wait, stop_wait):
                if not task.done():
                    task.cancel()
            for task in (cancel_wait, stop_wait):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ── After-write review (TD-5151) ────────────────────────────────

    @property
    def review_in_flight(self) -> bool:
        """True while an after-write review runs, so Stop reaches it."""
        return self._open_reviews > 0

    @contextlib.contextmanager
    def review_open(self) -> Iterator[None]:
        """Mark an after-write review as running for the ``with`` block."""
        self._open_reviews += 1
        try:
            yield
        finally:
            self._open_reviews -= 1

    async def wait_for_stop_turn(self) -> None:
        """Block until Stop lands. Cancel does not end this wait."""
        await self._stop_turn_event.wait()

    # ── Approvals (TD-802) ──────────────────────────────────────────

    async def _config_writable(self) -> bool:
        """True when TST Desk may save into the workspace config now (TD-5113)."""
        store = self.trust_store
        if store is None:
            return True
        return await asyncio.to_thread(store.writable, self.workspace_path)

    async def request_approval(
        self,
        tool_call_id: str,
        tool: Tool,
        arguments: dict[str, Any],
        decision_class: DecisionClass,
        summary: str,
        reason: str,
    ) -> ApprovalOutcome:
        """Park a tool call awaiting user approval.

        Logs the ``approval_request`` event (so reattaching clients replay
        it), parks the session in ``awaiting_approval``, and awaits a bare
        future — no spin, no poll.  With ``approval_timeout_seconds`` set,
        expiry is treated as a denial.  The session state is re-entrant:
        concurrent pending approvals share one ``awaiting_approval``
        transition, and ``running`` resumes when the last one resolves.
        """
        from .protocol import ApprovalRequest as ApprovalRequestEvent

        fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = PendingApproval(
            future=fut, tool=tool, arguments=arguments, decision_class=decision_class
        )
        # The rule "always allow" would write (TD-803), surfaced on the
        # card so the user sees it before committing to save it.  None for
        # a class-C call — the boundary can never be always-allowed — and
        # while the workspace config is not in force (TD-5113): the rule
        # could not be saved, so the card offers approve once instead.
        # Nor when a stricter rule is saved for this same call (TD-5149):
        # Always allow would have to replace the user's ask or never rule.
        proposed = propose_always_allow(tool, arguments, decision_class, Path(self.workspace_path))
        if proposed is not None and stricter_rule(self.policy, proposed) is not None:
            proposed = None
        if proposed is not None and not await self._config_writable():
            proposed = None
        await self.event_log.add(
            ApprovalRequestEvent(
                session_id=self.id,
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                arguments=arguments,
                decision_class=cast(Any, decision_class.value),
                summary=summary,
                reason=reason,
                proposed_always_allow=rule_summary(proposed) if proposed is not None else None,
                seq=1,
            )
        )
        if self._state != "awaiting_approval":
            await self.set_state("awaiting_approval", reason=reason)

        return await self._await_approval_resolution(tool_call_id, fut)

    async def _await_approval_resolution(
        self,
        tool_call_id: str,
        fut: asyncio.Future[tuple[bool, str | None]],
    ) -> ApprovalOutcome:
        """Await a parked approval with the configured timeout.

        Cleans up the pending record and restores ``running`` when the
        last approval resolves.  Timeout is treated as a denial (TD-802).
        Shared by tool-call (TD-802) and external-import (TD-505)
        approvals.
        """
        timeout = self.policy.approval_timeout_seconds
        timed_out = False
        try:
            if timeout is None:
                approved, detail = await fut
            else:
                try:
                    # Shield the future so expiry does not cancel it; a
                    # late approve/deny then resolves nothing (done check
                    # in resolve_approval) instead of raising.
                    approved, detail = await asyncio.wait_for(asyncio.shield(fut), timeout)
                except TimeoutError:
                    timed_out, approved, detail = True, False, None
        finally:
            self._pending_approvals.pop(tool_call_id, None)
            if not self._pending_approvals and self._state == "awaiting_approval":
                await self.set_state("running", reason="approval resolved")

        if approved:
            return ApprovalOutcome(True, "")
        if timed_out:
            return ApprovalOutcome(
                False, f"Approval timed out after {timeout}s — treated as denial"
            )
        return ApprovalOutcome(False, f"Denied by user: {detail}" if detail else "Denied by user")

    async def request_import_approval(self, path: Path) -> ApprovalOutcome:
        """Park the session awaiting approval to read an external import (TD-505).

        Reuses the same pending-future machinery as tool-call approvals, so
        ``approve``/``deny`` from any attached client resolves it and a
        disconnect leaves it parked.  The ``approval_request`` event carries
        a synthetic ``tool_call_id`` and ``tool_name="external_import"``; the
        class is always C (an untrusted-file-read), so ``always_allow`` is
        never offered.
        """
        from .protocol import ApprovalRequest as ApprovalRequestEvent

        tool_call_id = f"external-import:{path}"
        fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = PendingApproval(
            future=fut,
            tool=None,
            arguments={"path": str(path)},
            decision_class=DecisionClass.C,
        )
        reason = "import from outside the workspace"
        await self.event_log.add(
            ApprovalRequestEvent(
                session_id=self.id,
                tool_call_id=tool_call_id,
                tool_name="external_import",
                arguments={"path": str(path)},
                decision_class="C",
                summary=f"Read {path}",
                reason=reason,
                proposed_always_allow=None,
                seq=1,
            )
        )
        if self._state != "awaiting_approval":
            await self.set_state("awaiting_approval", reason=reason)

        outcome = await self._await_approval_resolution(tool_call_id, fut)
        # Emit a tool_result for the synthetic id so clients clear the
        # approval card and resolve the timeline entry — the approval store
        # only splices a card on a tool_result matching its tool_call_id,
        # and this id never reaches the dispatcher, so without this the
        # card would stick for the rest of the session.
        await self.event_log.add(
            ToolResult(
                session_id=self.id,
                tool_call_id=tool_call_id,
                status="success" if outcome.approved else "error",
                output=(
                    f"Approved external import: {path}" if outcome.approved else outcome.message
                ),
                error_code=None if outcome.approved else "approval_denied",
                seq=1,
            )
        )
        return outcome

    async def request_acp_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        summary: str,
        reason: str,
    ) -> ApprovalOutcome:
        """Park a Grok ACP permission request on the existing approval card.

        Grok has already classified the call; TST Desk asks the human
        with the same card native tools use. Always-allow is not offered
        here — Grok's own permission mode / skip-all covers that.
        """
        from .protocol import ApprovalRequest as ApprovalRequestEvent

        fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending_approvals[tool_call_id] = PendingApproval(
            future=fut,
            tool=None,
            arguments=arguments,
            decision_class=DecisionClass.B,
        )
        await self.event_log.add(
            ApprovalRequestEvent(
                session_id=self.id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                decision_class="B",
                summary=summary,
                reason=reason,
                proposed_always_allow=None,
                seq=1,
            )
        )
        if self._state != "awaiting_approval":
            await self.set_state("awaiting_approval", reason=reason)
        outcome = await self._await_approval_resolution(tool_call_id, fut)
        await self.event_log.add(
            ToolResult(
                session_id=self.id,
                tool_call_id=tool_call_id,
                status="success" if outcome.approved else "error",
                output="Approved" if outcome.approved else outcome.message,
                error_code=None if outcome.approved else "approval_denied",
                seq=1,
            )
        )
        return outcome

    def get_pending_approval(self, tool_call_id: str) -> PendingApproval | None:
        """Return the metadata for a still-parked approval, or ``None``.

        The daemon uses this to generate the always-allow rule (TD-803).
        An already-resolved future still sits in the map until the waiter
        pops it; treat that as gone so a second client cannot always-allow
        after another client already won (TD-3702 / TD-1014).
        """
        pending = self._pending_approvals.get(tool_call_id)
        if pending is None or pending.future.done():
            return None
        return pending

    def resolve_approval(
        self, tool_call_id: str, approved: bool, detail: str | None = None
    ) -> bool:
        """Resolve a parked approval (approve/deny from any attached client).

        Returns ``False`` when no such approval is pending (unknown id,
        already resolved, or timed out) so the daemon can answer with a
        typed error.
        """
        pending = self._pending_approvals.get(tool_call_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result((approved, detail))
        return True

    def resolve_skippable_approvals(self) -> int:
        """Approve every parked call (TD-804 / TD-806).

        Skip-all is skip-everything: Class C is released too. A
        ``never`` rule never parks, so it is not in this map. Returns
        how many calls were released.
        """
        released = 0
        for tool_call_id, _pending in list(self._pending_approvals.items()):
            if self.resolve_approval(tool_call_id, True):
                released += 1
        return released

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    async def wait_for_cancel(self) -> None:
        """Block until cancellation is requested."""
        await self._cancel_event.wait()

    # ── Cap pause (TD-707) ──────────────────────────────────────────

    async def pause_at_cap(self, reason: str) -> None:
        """Enter the paused-at-cap state (a fault report, not approval).

        The loop parks in ``wait_for_resume`` until the user raises the
        cap and the daemon signals ``resume``.
        """
        self._resume_event.clear()
        await self.set_state("paused", reason=reason)
        # A stop or cancel that landed while the state was changing
        # must not leave the loop parked.
        if self.cancel_requested or self.stop_turn_requested:
            self._resume_event.set()

    async def wait_for_resume(self) -> None:
        """Block until the user resumes the session (or it is cancelled).

        Waits without spinning or polling; the daemon's ``resume``
        handler signals the event. A stop or cancel releases the wait
        too, and the loop decides which one it was.
        """
        if self.cancel_requested or self.stop_turn_requested:
            self._resume_event.set()
        await self._resume_event.wait()

    async def resume(self) -> None:
        """Resume from a cap pause; the loop re-checks caps next call."""
        self._resume_event.set()
        if self._state == "paused":
            await self.set_state("running", reason="resumed after cap adjustment")

    # ── User message queue ──────────────────────────────────────────

    @property
    def pending_user_messages(self) -> int:
        """Messages enqueued but not yet dequeued by the agent loop.

        Read at dequeue time for the ``turn started`` log (TD-1713) — a
        user who sent three messages while the loop was busy should see
        that backlog named in the logs, not just the head one.
        """
        return self._user_message_queue.qsize()

    async def add_user_message(self, content: str | QueuedUserMessage) -> None:
        """Enqueue a user message for the agent loop to process.

        It is written to the session's queue file before the loop can take
        it, so a restart before its turn starts keeps it (TD-5155).
        """
        self._open_turns += 1
        queued = QueuedUserMessage.plain(content) if isinstance(content, str) else content
        async with self._queue_lock:
            self._waiting.append(queued)
            await self._write_queue()
        self._user_message_queue.put_nowait(queued)

    async def user_turn_logged(self, queued: QueuedUserMessage) -> None:
        """*queued*'s user_turn and conversation are saved: it is not waiting (TD-5155).

        Called after both reach disk, so a stop in between finds the
        message in the log (the restore drops it by turn_id) or in the
        queue file, never in neither.
        """
        async with self._queue_lock:
            kept = [m for m in self._waiting if m.id != queued.id]
            if len(kept) == len(self._waiting):
                return
            self._waiting = kept
            await self._write_queue()

    async def restore_held(self, held: list[QueuedUserMessage]) -> None:
        """Hold what a restart brought back; nothing is sent (TD-5155)."""
        async with self._queue_lock:
            self.held_messages = list(held)
            await self._write_queue()

    async def send_held(self, queued_id: str) -> bool:
        """Queue a held message for the loop, as if just sent. False if not held."""
        async with self._queue_lock:
            found = next((m for m in self.held_messages if m.id == queued_id), None)
            if found is None:
                return False
            self.held_messages = [m for m in self.held_messages if m.id != queued_id]
            self._waiting.append(found)
            self._open_turns += 1
            await self._write_queue()
        # The list goes out before the loop can log the turn it starts.
        await self.log_held_messages()
        self._user_message_queue.put_nowait(found)
        return True

    async def remove_held(self, queued_id: str) -> bool:
        """Drop a held message unsent. False if not held."""
        async with self._queue_lock:
            kept = [m for m in self.held_messages if m.id != queued_id]
            if len(kept) == len(self.held_messages):
                return False
            self.held_messages = kept
            await self._write_queue()
        await self.log_held_messages()
        return True

    async def log_held_messages(self) -> None:
        """Log the held list whole, so the latest one in a replay is the list."""
        from .protocol import QueuedMessageEntry, QueuedMessages

        await self.event_log.add(
            QueuedMessages(
                session_id=self.id,
                messages=[
                    QueuedMessageEntry(
                        queued_id=m.id, content=m.display, images_dropped=m.images_dropped
                    )
                    for m in self.held_messages
                ],
                seq=1,
            )
        )

    async def _write_queue(self) -> None:
        """Save held then waiting messages. Caller holds ``_queue_lock``."""
        if self._queue_hook is not None:
            await self._queue_hook([*self.held_messages, *self._waiting])

    async def wait_for_user_message(self) -> QueuedUserMessage | None:
        """Wait for the next user message.

        Returns the queued turn, or ``None`` if the session is cancelled
        while waiting.  Uses a short poll so cancellation is responsive.
        """
        while True:
            if self._cancel_event.is_set():
                return None
            try:
                return self._user_message_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

    # ── Conversation fork (TD-1708) ─────────────────────────────────

    def _find_user_turn(self, user_index: int) -> int | str:
        """Where the window's user turn *user_index* is in the conversation.

        The window numbers user turns from the start of the session, and a
        compaction folds some of them into a summary that says how many it
        stands for (TD-5109), so the number is mapped through
        :func:`window_user_turns`.  Returns the message index, or the
        refusal code: ``user_turn_not_found`` or, for a turn inside a
        summary, ``user_turn_compacted`` (TD-5147).
        """
        turns = window_user_turns(self.conversation)
        if user_index < 0 or user_index >= len(turns):
            return "user_turn_not_found"
        position = turns[user_index]
        return "user_turn_compacted" if position is None else position

    def _copy_conversation(self) -> list[ChatMessage]:
        return deepcopy(self.conversation)

    def _save_active_sibling(self, user_index: int) -> None:
        cursor = self._branch_cursor.get(user_index, 0)
        siblings = self._branches.setdefault(user_index, [])
        while len(siblings) <= cursor:
            siblings.append([])
        siblings[cursor] = self._copy_conversation()

    def _emit_reset(self, user_index: int, content: str) -> ConversationReset:
        siblings = self._branches.get(user_index, [[]])
        return ConversationReset(
            session_id=self.id,
            user_index=user_index,
            sibling_index=self._branch_cursor.get(user_index, 0),
            sibling_count=max(len(siblings), 1),
            content=content,
            seq=1,
        )

    async def fork_from(self, user_index: int, content: str) -> ConversationReset | str:
        """Replace the window's user turn *user_index* and drop everything after it.

        Siblings stay keyed by the window's number, which no compaction
        changes; only the cut is mapped onto the conversation (TD-5147).
        Returns the reset event, or an error code.
        """
        text = content.strip()
        if text == "":
            return "empty_content"
        if self.turn_in_flight:
            return "turn_in_progress"
        cut = self._find_user_turn(user_index)
        if isinstance(cut, str):
            return cut
        self._save_active_sibling(user_index)
        siblings = self._branches[user_index]
        siblings.append([])
        self._branch_cursor[user_index] = len(siblings) - 1
        self.conversation[cut:] = []
        self._drain_user_queue()
        await self.add_user_message(text)
        return self._emit_reset(user_index, text)

    async def compact_now(self, guidance: str = "", user_index: int | None = None) -> str | None:
        """Compact on request (TD-5109).  Returns an error code, or None.

        ``user_index`` (0-based among the session's user turns, as the
        window numbers them) summarizes from that turn on; None summarizes
        everything before the last user turn.  The number is mapped through
        the summaries already in the conversation, each of which says how
        many user turns it stands for, so it holds after any compaction
        and after a restart.
        """
        from .protocol import ContextCompacted

        if self.turn_in_flight:
            return "turn_in_progress"
        meter = self.context_meter
        if meter is None:
            return "nothing_to_compact"
        position: int | None = None
        if user_index is not None:
            found = self._find_user_turn(user_index)
            if isinstance(found, str):
                return found
            position = found
        result = compact_on_request(
            self.conversation,
            meter.counter,
            meter.tools,
            guidance=guidance,
            from_position=position,
        )
        if result is None:
            return "nothing_to_compact"
        compacted, stats = result
        self.conversation[:] = compacted
        await self.event_log.add(
            ContextCompacted(
                session_id=self.id,
                dropped_messages=stats.dropped_messages,
                kept_messages=stats.kept_messages,
                tokens_before=stats.tokens_before,
                tokens_after=stats.tokens_after,
                trigger="manual" if user_index is None else "from_here",
                guidance=guidance.strip() or None,
                seq=1,
            )
        )
        await self.report_context_usage(stats.tokens_after)
        log.info(
            "context compacted on request",
            extra={
                "extra_fields": {
                    "session_id": self.id,
                    "from_user_index": user_index,
                    "dropped_messages": stats.dropped_messages,
                    "tokens_before": stats.tokens_before,
                    "tokens_after": stats.tokens_after,
                }
            },
        )
        return None

    async def report_context_usage(self, used_tokens: int) -> None:
        """Send the gauge its figure for the next request (TD-5109)."""
        from .compaction import budget_threshold
        from .protocol import ContextUsage

        meter = self.context_meter
        if meter is None:
            return
        await self.event_log.add(
            ContextUsage(
                session_id=self.id,
                tier=meter.tier,
                used_tokens=used_tokens,
                context_window=meter.tier_cfg.context_window,
                compact_at=budget_threshold(meter.tier_cfg),
                seq=1,
            )
        )

    def _drain_user_queue(self) -> None:
        # The queue file follows on the next write: fork_from adds its
        # message next, and set_branch writes the empty queue itself.
        self._waiting = []
        while True:
            try:
                self._user_message_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._open_turns = max(0, self._open_turns - 1)

    def snapshot_branches(self) -> None:
        """Refresh the active sibling after a turn lands (TD-1708)."""
        for user_index in list(self._branches):
            self._save_active_sibling(user_index)

    async def set_branch(self, user_index: int, sibling_index: int) -> ConversationReset | str:
        """Restore a sibling snapshot at the window's user turn *user_index*.

        The arrows sit on a turn of the live conversation, so a turn that
        has since been folded into a summary is refused like an edit of it
        (TD-5147).  The reset carries that turn's text from the restored
        snapshot, found by the same mapping.
        """
        from .provider import content_as_text

        if self.turn_in_flight:
            return "turn_in_progress"
        siblings = self._branches.get(user_index)
        if siblings is None or sibling_index < 0 or sibling_index >= len(siblings):
            return "branch_not_found"
        found = self._find_user_turn(user_index)
        if isinstance(found, str):
            return found
        self._save_active_sibling(user_index)
        self._branch_cursor[user_index] = sibling_index
        self.conversation[:] = deepcopy(siblings[sibling_index])
        self._drain_user_queue()
        async with self._queue_lock:
            await self._write_queue()
        position = self._find_user_turn(user_index)
        text = (
            ""
            if isinstance(position, str)
            else content_as_text(self.conversation[position].content)
        )
        return self._emit_reset(user_index, text)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_path": self.workspace_path,
            "state": self._state,
            "event_count": self.event_log.last_seq,
        }


# Terminal states (TD-1711): no outgoing transitions, so nothing will ever
# consume a user message again — the daemon refuses sends to these rather
# than enqueueing into the void. Derived from the transition table so the
# two can never drift.
TERMINAL_STATES: frozenset[str] = frozenset(
    state for state, allowed in Session.VALID_TRANSITIONS.items() if not allowed
)


# ── Placeholder loop ───────────────────────────────────────────────────


async def _placeholder_loop(session: Session) -> None:
    """Placeholder agent loop — waits for cancellation.

    Replaced by the real agent loop in E4 (TD-401).
    """
    await session.wait_for_cancel()


# ── Session runner ─────────────────────────────────────────────────────


class SessionRunner:
    """Owns the session loop as an asyncio task.

    The runner is created by the daemon and runs independently of any
    client connection. The connection is only a viewer — closing it
    does not affect the session (prime directive §2.5).
    """

    def __init__(
        self,
        session: Session,
        loop_factory: Callable[[Session], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self._loop_factory = loop_factory or _placeholder_loop
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the session loop as a background task.

        Waits until the session has transitioned to 'running' before returning,
        so callers can rely on the state being set.
        """
        if self._task is not None:
            raise SessionError("Session runner already started")
        self._task = asyncio.create_task(self._run())
        # Yield control briefly so the task can start and set_state('running')
        for _ in range(50):
            if self.session.state == "running":
                return
            await asyncio.sleep(0.01)

    async def _run(self) -> None:
        """Run the session loop with lifecycle management."""
        try:
            await self.session.set_state("running")
            await self._loop_factory(self.session)
            # If the loop exited normally (not cancelled), check if complete
            if not self.session.cancel_requested and self.session.state == "running":
                await self.session.set_state("complete")
        except asyncio.CancelledError:
            if self.session.state not in ("complete", "failed", "cancelled"):
                await self.session.set_state("cancelled", reason="task cancelled")
        except Exception as e:
            log.exception(
                "session loop failed",
                extra={
                    "extra_fields": {
                        "session_id": self.session.id,
                        "error": str(e),
                    }
                },
            )
            if self.session.state not in ("complete", "failed", "cancelled"):
                await self.session.set_state("failed", reason=str(e))

    async def cancel(self, reason: str = "cancelled by user") -> None:
        """Cancel the session and its loop."""
        await self.session.cancel(reason)
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()


# ── Session registry ───────────────────────────────────────────────────


class SessionRegistry:
    """Registry of active sessions and their runners.

    Thread-safe via asyncio.Lock. Supports lookup, listing, and cancellation.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._runners: dict[str, SessionRunner] = {}
        self._lock = asyncio.Lock()

    async def create(self, workspace_path: str) -> Session:
        """Create a new session and register it."""
        session = Session(workspace_path)
        async with self._lock:
            self._sessions[session.id] = session
        log.info(
            "session created",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "workspace_path": workspace_path,
                }
            },
        )
        return session

    async def restore(
        self,
        session_id: str,
        workspace_path: str,
        state: str = "interrupted",
    ) -> Session:
        """Re-insert a persisted session after a restart (TD-1002).

        ``state`` is the honest starting point. A tombstone stays
        ``interrupted`` with no runner. A revive restores as ``idle``
        and the daemon attaches a runner afterwards.
        """
        session = Session.restore(session_id, workspace_path, state)
        async with self._lock:
            self._sessions[session.id] = session
        return session

    async def register_runner(self, session_id: str, runner: SessionRunner) -> None:
        """Register a runner for an existing session."""
        async with self._lock:
            self._runners[session_id] = runner

    def get(self, session_id: str) -> Session | None:
        """Look up a session by ID."""
        return self._sessions.get(session_id)

    def get_runner(self, session_id: str) -> SessionRunner | None:
        """Look up a runner by session ID."""
        return self._runners.get(session_id)

    async def cancel(self, session_id: str, reason: str = "cancelled by user") -> None:
        """Cancel a session and its runner. *reason* is the ``cancelled`` state's."""
        runner = self._runners.get(session_id)
        if runner:
            await runner.cancel(reason)

    async def remove(self, session_id: str) -> None:
        """Remove a session and its runner from the registry."""
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._runners.pop(session_id, None)

    async def list_sessions(self) -> list[Session]:
        """Return all registered sessions."""
        async with self._lock:
            return list(self._sessions.values())

    @property
    def count(self) -> int:
        return len(self._sessions)
