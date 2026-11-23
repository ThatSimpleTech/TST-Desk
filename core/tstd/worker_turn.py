"""How a turn waits for its background workers (TD-5299).

A background ``delegate`` worker belongs to the turn that started it. When
one settles, its note goes in before the parent's next model call, at the
same tool-batch boundary a steer joins (TD-5112). When the parent model
stops calling tools while a worker is alive, the turn does not end: the
loop waits, with no model call, for a worker to settle, a steer, or Stop,
then gives the model the note and calls it again.
"""

from __future__ import annotations

import asyncio
import contextlib

from .provider import ChatMessage
from .session import Session


async def inject_worker_notes(session: Session, messages: list[ChatMessage]) -> int:
    """Append a note for every settled worker the model has not heard about."""
    notes = session.workers.take_notes()
    for note in notes:
        messages.append(ChatMessage(role="user", content=note, steer=True))
    if notes:
        await session.conversation_changed()
    return len(notes)


def owes_the_turn(session: Session) -> bool:
    """Whether the turn must stay open: a worker is alive or its note is owed."""
    return session.workers.owed()


async def wait_for_workers(session: Session) -> None:
    """Until a worker settles, a steer arrives, or the turn is stopped.

    Returns at once when a note is already owed or nothing is alive.
    """
    jobs = session.workers
    while True:
        if any(not j.live and not j.delivered for j in jobs.all()) or not jobs.live():
            return
        if session.steer_arrived.is_set() or session.stop_turn_requested:
            return
        if session.cancel_requested:
            return
        waits = [
            asyncio.ensure_future(jobs.wait_change()),
            asyncio.ensure_future(session.steer_arrived.wait()),
            asyncio.ensure_future(session.wait_for_turn_abort()),
        ]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
            for wait in waits:
                with contextlib.suppress(asyncio.CancelledError):
                    await wait
