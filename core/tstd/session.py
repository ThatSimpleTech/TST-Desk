"""Session model, event log, and registry.

The session owns the agent loop. The socket is only a viewer.
The daemon owns sessions; the window is only a viewer (prime directive §2.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from .autonomy.classifier import DecisionClass
from .boundary_config import BoundaryConfig
from .logging import get_logger, redact_secrets, redact_structure
from .policy import ApprovalOutcome, PolicyConfig, propose_always_allow
from .protocol import DaemonEvent, Error, PolicyRuleSummary, ShellOutput, ToolCall, ToolResult
from .protocol import SessionState as SessionStateEvent

if TYPE_CHECKING:
    from .cost import CostTracker
    from .router import TierRouter
    from .tools.registry import Tool

log = get_logger("tstd.session")


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
    """
    if isinstance(event, ToolCall):
        return event.model_copy(update={"arguments": redact_structure(event.arguments)})
    if isinstance(event, ToolResult):
        update: dict[str, Any] = {"output": redact_secrets(event.output)}
        if event.diff is not None:
            update["diff"] = redact_secrets(event.diff)
        return event.model_copy(update=update)
    if isinstance(event, ShellOutput):
        return event.model_copy(update={"chunk": redact_secrets(event.chunk)})
    if isinstance(event, Error):
        return event.model_copy(update={"message": redact_secrets(event.message)})
    if isinstance(event, SessionStateEvent) and event.reason is not None:
        return event.model_copy(update={"reason": redact_secrets(event.reason)})
    return event


# ── Event log ──────────────────────────────────────────────────────────


class EventSubscriber(Protocol):
    """Protocol for event subscribers."""

    async def __call__(self, event: DaemonEvent, log: SessionEventLog) -> None: ...


class SessionEventLog:
    """Append-only event log with monotonic seq per session.

    Every event is stamped with the next seq number on insertion.
    The log is append-only — no mutations, no deletions.

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
        return [e for e in self._events if e.seq >= seq]

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
    def all_events(self) -> list[DaemonEvent]:
        return list(self._events)


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
        # `interrupted` is a terminal tombstone: the daemon died while the
        # session was alive. No event log survived, so it can never resume.
        "interrupted": set(),
    }

    def __init__(self, workspace_path: str) -> None:
        self.id = str(uuid.uuid4())
        self.workspace_path = workspace_path
        self._state = "idle"
        self.event_log = SessionEventLog()
        self._cancel_event = asyncio.Event()
        self._user_message_queue: asyncio.Queue[str] = asyncio.Queue()
        self._resume_event = asyncio.Event()
        # Workspace boundary (TD-706), resolved by the daemon on open.
        self.boundary_config = BoundaryConfig()
        # Tier router (TD-1006), attached by the daemon on open so a
        # ``set_tier`` message reaches the loop's router. ``None`` on a
        # restored tombstone — its loop is gone for good.
        self.router: TierRouter | None = None
        # Cost tracker (TD-1201), attached by the loop at startup so the
        # daemon can answer cache-state queries. Same tombstone rule.
        self.cost_tracker: CostTracker | None = None
        # Approval policy (TD-801), resolved by the daemon on open.
        self.policy = PolicyConfig()
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

    @classmethod
    def restore(
        cls,
        session_id: str,
        workspace_path: str,
        state: str = "interrupted",
    ) -> Session:
        """Recreate a session tombstone after a daemon restart (TD-1002).

        The persisted registry knows the session existed and its workspace,
        but the in-memory event log did not survive, so the loop can never
        resume. A session that was terminal before the crash keeps its
        terminal ``state``; anything still alive comes back ``interrupted``.
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
        )
        await self.event_log.add(event)

    async def cancel(self) -> None:
        """Request cancellation of this session."""
        self._cancel_event.set()
        # Wake any parked approvals so their dispatchers unwind.
        for pending in self._pending_approvals.values():
            pending.future.cancel()
        if self._state in ("running", "awaiting_approval", "paused"):
            await self.set_state("cancelled", reason="cancelled by user")
        # Wake a loop parked in wait_for_resume so it observes cancellation.
        self._resume_event.set()

    # ── Approvals (TD-802) ──────────────────────────────────────────

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
        # a class-C call — the boundary can never be always-allowed.
        proposed = propose_always_allow(tool, arguments, decision_class, Path(self.workspace_path))
        await self.event_log.add(
            ApprovalRequestEvent(
                session_id=self.id,
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                arguments=arguments,
                decision_class=cast(Any, decision_class.value),
                summary=summary,
                reason=reason,
                proposed_always_allow=(
                    PolicyRuleSummary(
                        tool=proposed.tool, args=proposed.args, effect=proposed.effect
                    )
                    if proposed is not None
                    else None
                ),
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

    def get_pending_approval(self, tool_call_id: str) -> PendingApproval | None:
        """Return the metadata for a parked approval, or ``None`` if none.

        The daemon uses this to generate the always-allow rule (TD-803).
        """
        return self._pending_approvals.get(tool_call_id)

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

    async def wait_for_resume(self) -> None:
        """Block until the user resumes the session (or it is cancelled).

        Waits without spinning or polling; the daemon's ``resume``
        handler signals the event.
        """
        await self._resume_event.wait()

    async def resume(self) -> None:
        """Resume from a cap pause; the loop re-checks caps next call."""
        self._resume_event.set()
        if self._state == "paused":
            await self.set_state("running", reason="resumed after cap adjustment")

    # ── User message queue ──────────────────────────────────────────

    async def add_user_message(self, content: str) -> None:
        """Enqueue a user message for the agent loop to process."""
        self._user_message_queue.put_nowait(content)

    async def wait_for_user_message(self) -> str | None:
        """Wait for the next user message.

        Returns the message content, or ``None`` if the session is
        cancelled while waiting.  Uses a short poll so cancellation is
        responsive.
        """
        while True:
            if self._cancel_event.is_set():
                return None
            try:
                return self._user_message_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_path": self.workspace_path,
            "state": self._state,
            "event_count": self.event_log.last_seq,
        }


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

    async def cancel(self) -> None:
        """Cancel the session and its loop."""
        await self.session.cancel()
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
        """Re-insert a persisted session tombstone after a restart (TD-1002).

        The session gets no runner — there is no event log to resume and
        nothing to supervise.
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

    async def cancel(self, session_id: str) -> None:
        """Cancel a session and its runner."""
        runner = self._runners.get(session_id)
        if runner:
            await runner.cancel()

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
