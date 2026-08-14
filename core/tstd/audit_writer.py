"""Audit writer (TD-902) — feeds the append-only store from live sessions.

Two feeds, one queue:

1. **Event subscription** — the writer subscribes to each session's
   append-only event log and records tool calls, results, turns, and
   decisions from the event stream. The loop does not know the store
   exists, so recording can never change its behavior.
2. **Cost-tracker listener** — ``CostTracker.add_listener`` (TD-304 seam)
   hands every model call to :meth:`AuditWriter.record_model_call`, so
   per-call token and cost detail reaches the store even though no
   protocol event carries it.

Non-blocking by construction (criterion 2): producers only mutate dicts
and ``put_nowait`` onto an unbounded queue; a single drain task performs
all SQLite I/O via ``asyncio.to_thread``. A write failure never stalls a
producer either — it is reported loudly instead (criterion 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from .audit import AuditStore, DecisionClass, ToolCallStatus
from .logging import get_logger
from .protocol import DaemonEvent, DecisionLogged, Error, SessionState, ToolResult, TurnComplete
from .protocol import ToolCall as ToolCallEvent
from .session import EventSubscriber, Session, SessionEventLog

if TYPE_CHECKING:
    from .cost import CallRecord

log = get_logger("tstd.audit")

# One unit of store work, tagged with the session it belongs to so a
# failure can be reported on the right timeline. ``None`` is session-
# independent (should not occur — every flow here is session-scoped).
# The callable's return value (a row id) is discarded by the drain.
_Op = tuple[str | None, Callable[[], object]]


class ModelCallSink(Protocol):
    """What the agent loop needs from the audit writer (TD-902)."""

    def record_model_call(
        self, session_id: str, record: CallRecord, is_classifier: bool
    ) -> None: ...


@dataclass
class _PendingCall:
    """A ToolCall event awaiting its ToolResult."""

    name: str
    arguments: dict[str, Any]
    decision_class: str | None


class AuditWriter:
    """Serializes audit writes from all sessions onto one store.

    Create per daemon, ``start()`` when the event loop runs, and
    ``attach_session()`` for each session. ``close()`` drains the queue
    before closing the store so shutdown loses nothing already queued.
    """

    def __init__(self, store: AuditStore) -> None:
        self._store = store
        self._queue: asyncio.Queue[_Op] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, Session] = {}
        self._pending_calls: dict[str, dict[str, _PendingCall]] = {}
        self._pending_model_calls: dict[str, list[CallRecord]] = {}
        self._turn_counters: dict[str, int] = {}
        # Edge-triggered "audit incomplete" reporting: the user is told
        # on the first failure after a run of successes, not per write.
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """True when store writes are failing — read by the doctor view later."""
        return self._degraded

    @property
    def backlog(self) -> int:
        """Queued-but-unwritten operations (observability, tests)."""
        return self._queue.qsize()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the drain task. Call from a running event loop."""
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain())

    async def close(self) -> None:
        """Flush pending work, drain the queue, stop the drain task, and close
        the store."""
        # Model calls buffered for turns that never completed (cancelled
        # mid-stream, daemon shutdown) must not be lost — daemon shutdown
        # flushes state (TD-201). Linked turn_id is unknown; record unlinked.
        for sid in list(self._pending_model_calls):
            self._flush_pending_model_calls(sid)
        if self._drain_task is not None:
            await self._queue.join()
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None
        await asyncio.to_thread(self._store.close)

    # ── Producers (event loop thread, never block) ─────────────────────

    def attach_session(self, session: Session) -> None:
        """Record the session and subscribe to its event log."""
        self._sessions[session.id] = session
        workspace = session.workspace_path
        sid = session.id
        self._enqueue(sid, lambda: self._store.append_session(sid, workspace, time.time()))
        session.event_log.subscribe(self._make_subscriber(session))

    def record_model_call(self, session_id: str, record: CallRecord, is_classifier: bool) -> None:
        """Sink for ``CostTracker`` listeners (TD-902 feed 2).

        Classifier calls are not part of any turn, so they are written
        immediately with ``turn_id=None``. Turn calls are buffered and
        flushed together with the turn row so their ``turn_id`` foreign
        key can be set at insert time — the store is append-only, so a
        backfill pass is not an option.
        """
        if is_classifier:
            rec = record
            self._enqueue(
                session_id,
                lambda: _write_model_call(self._store, session_id, None, rec, True),
            )
        else:
            self._pending_model_calls.setdefault(session_id, []).append(record)

    def _flush_pending_model_calls(self, sid: str) -> None:
        """Write buffered model calls for a turn that ended without
        ``TurnComplete`` (cancel mid-stream, loop failure, shutdown)."""
        buffered = self._pending_model_calls.pop(sid, [])
        if not buffered:
            return

        def _op() -> None:
            for rec in buffered:
                _write_model_call(self._store, sid, None, rec, is_classifier=False)

        self._enqueue(sid, _op)

    # ── Event subscription ─────────────────────────────────────────────

    def _make_subscriber(self, session: Session) -> EventSubscriber:
        sid = session.id

        async def _on_event(event: DaemonEvent, log: SessionEventLog) -> None:
            if isinstance(event, ToolCallEvent):
                self._on_tool_call(sid, event)
            elif isinstance(event, ToolResult):
                self._on_tool_result(sid, event)
            elif isinstance(event, TurnComplete):
                self._on_turn_complete(sid, event)
            elif isinstance(event, DecisionLogged):
                self._on_decision(sid, event)
            elif isinstance(event, SessionState) and event.state in (
                "complete",
                "failed",
                "cancelled",
            ):
                # A turn that ends without TurnComplete (cancel mid-stream,
                # loop failure) still spent money — flush its buffered model
                # calls unlinked rather than lose them from the record.
                self._flush_pending_model_calls(sid)
            # Other event types carry no audit obligation.

        return _on_event

    def _on_tool_call(self, sid: str, event: ToolCallEvent) -> None:
        self._pending_calls.setdefault(sid, {})[event.tool_call_id] = _PendingCall(
            name=event.name,
            arguments=dict(event.arguments),
            decision_class=event.decision_class,
        )

    def _on_tool_result(self, sid: str, event: ToolResult) -> None:
        pending = self._pending_calls.get(sid, {}).pop(event.tool_call_id, None)
        if pending is None:
            # A result whose call the writer never saw (attached after the
            # call, or the call was dropped). Recorded, not skipped —
            # losing an action from the audit is worse than a blank name.
            log.warning(
                "tool result without a pending call",
                extra={"extra_fields": {"session_id": sid, "tool_call_id": event.tool_call_id}},
            )
            pending = _PendingCall(name="<unknown>", arguments={}, decision_class=None)
        # A boundary refusal reaches the result with class C and status
        # error (TD-602 forces the class); store it distinctly so Class C
        # refusals are queryable on their own.
        status: ToolCallStatus = (
            "refused" if event.status == "error" and pending.decision_class == "C" else event.status
        )
        output = event.output
        self._enqueue(
            sid,
            lambda: self._store.append_tool_call(
                session_id=sid,
                tool_call_id=event.tool_call_id,
                name=pending.name,
                arguments=pending.arguments,
                decision_class=cast_decision_class(pending.decision_class),
                status=status,
                result_output=output,
                ts=time.time(),
            ),
        )

    def _on_turn_complete(self, sid: str, event: TurnComplete) -> None:
        index = self._turn_counters.get(sid, 0) + 1
        self._turn_counters[sid] = index
        buffered = self._pending_model_calls.pop(sid, [])

        def _op() -> None:
            turn_id = self._store.append_turn(
                session_id=sid,
                turn_index=index,
                tier=event.tier,
                tokens=event.tokens,
                cost=event.cost,
                duration=event.duration,
                ts=time.time(),
            )
            for rec in buffered:
                _write_model_call(self._store, sid, turn_id, rec, is_classifier=False)

        self._enqueue(sid, _op)

    def _on_decision(self, sid: str, event: DecisionLogged) -> None:
        self._enqueue(
            sid,
            lambda: self._store.append_decision(
                session_id=sid,
                decision_class=event.decision_class,
                what=event.what,
                why=event.why,
                commit_sha=event.commit,
                ts=time.time(),
            ),
        )

    # ── Drain ──────────────────────────────────────────────────────────

    def _enqueue(self, session_id: str | None, op: Callable[[], object]) -> None:
        self._queue.put_nowait((session_id, op))

    async def _drain(self) -> None:
        while True:
            sid, op = await self._queue.get()
            try:
                await asyncio.to_thread(op)
                self._degraded = False
            except Exception as e:
                await self._report_failure(sid, e)
            finally:
                self._queue.task_done()

    async def _report_failure(self, sid: str | None, error: Exception) -> None:
        """Tell the user the audit trail is incomplete — loudly, once."""
        log.exception(
            "audit write failed",
            extra={"extra_fields": {"session_id": sid, "error": str(error)}},
        )
        if self._degraded:
            return  # already told; failing again adds no information
        self._degraded = True
        targets = [self._sessions[sid]] if sid in self._sessions else list(self._sessions.values())
        for session in targets:
            try:
                await session.event_log.add(
                    Error(
                        session_id=session.id,
                        code="audit_write_failed",
                        message=(
                            "Audit trail incomplete: writes to the audit database are "
                            f"failing ({error}). The session continues, but activity "
                            "is not being recorded."
                        ),
                        seq=1,  # overwritten by the event log
                    )
                )
            except Exception:
                # A dead session log must not kill the drain task too.
                log.exception(
                    "failed to report audit degradation",
                    extra={"extra_fields": {"session_id": session.id}},
                )


def cast_decision_class(value: str | None) -> DecisionClass | None:
    """Narrow an event's decision-class string to the store's literal type."""
    if value in ("A", "B", "C"):
        return cast(DecisionClass, value)
    return None


def _write_model_call(
    store: AuditStore,
    sid: str,
    turn_id: int | None,
    rec: CallRecord,
    is_classifier: bool,
) -> None:
    """Single insert path for every model-call row."""
    store.append_model_call(
        session_id=sid,
        turn_id=turn_id,
        tier=rec.tier,
        model=rec.model,
        prompt_tokens=rec.prompt_tokens,
        cached_prompt_tokens=rec.cached_prompt_tokens,
        completion_tokens=rec.completion_tokens,
        cost=rec.cost,
        is_classifier=is_classifier,
        ts=rec.timestamp.timestamp(),
    )
