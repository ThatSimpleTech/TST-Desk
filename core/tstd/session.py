"""Session model, event log, and registry.

The session owns the agent loop. The socket is only a viewer.
The daemon owns sessions; the window is only a viewer (prime directive §2.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Protocol, cast

from .boundary_config import BoundaryConfig
from .logging import get_logger
from .protocol import DaemonEvent
from .protocol import SessionState as SessionStateEvent

log = get_logger("tstd.session")


class SessionError(Exception):
    """Session-related error."""


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
    """

    # Valid state transitions: current_state → {allowed_next_states}
    VALID_TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "idle": {"running"},
        "running": {"awaiting_approval", "complete", "failed", "cancelled"},
        "awaiting_approval": {"running", "cancelled"},
        "complete": set(),
        "failed": set(),
        "cancelled": set(),
    }

    def __init__(self, workspace_path: str) -> None:
        self.id = str(uuid.uuid4())
        self.workspace_path = workspace_path
        self._state = "idle"
        self.event_log = SessionEventLog()
        self._cancel_event = asyncio.Event()
        self._user_message_queue: asyncio.Queue[str] = asyncio.Queue()
        # Workspace boundary (TD-706), resolved by the daemon on open.
        self.boundary_config = BoundaryConfig()

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
        if self._state in ("running", "awaiting_approval"):
            await self.set_state("cancelled", reason="cancelled by user")

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    async def wait_for_cancel(self) -> None:
        """Block until cancellation is requested."""
        await self._cancel_event.wait()

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
