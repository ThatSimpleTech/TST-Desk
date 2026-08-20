"""Run distill on graceful end (TD-2302).

Quit and End session share this path. Crash / force-quit never reaches
it, so they write nothing. Unchanged memory yields no event.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import ModelConfig
from .memory_distill import DistillProposal, DistillProvider, DistillTurn, distill_session
from .memory_store import memory_dir, memory_max_lines, replace_memory_file
from .protocol import MemoryFileDiff, MemoryProposal, TurnComplete
from .session import Session


@dataclass(frozen=True)
class DistillEmit:
    """A proposal ready to park and broadcast."""

    proposal_id: str
    proposal: DistillProposal
    event: MemoryProposal


def completed_turn_count(session: Session) -> int:
    """Successful user turns in this session's log."""
    return sum(
        1
        for event in session.event_log.all_events
        if isinstance(event, TurnComplete) and not event.failed
    )


def turns_from_conversation(session: Session) -> tuple[DistillTurn, ...]:
    out: list[DistillTurn] = []
    for msg in session.conversation:
        if msg.role not in {"user", "assistant"} or not msg.content:
            continue
        out.append(DistillTurn(msg.role, msg.content))
    return tuple(out)


def proposal_event(session_id: str, proposal_id: str, proposal: DistillProposal) -> MemoryProposal:
    return MemoryProposal(
        session_id=session_id,
        proposal_id=proposal_id,
        files=[
            MemoryFileDiff(
                action=change.action,
                path=change.relative.as_posix(),
                diff=change.diff,
                before=change.before,
                after=change.after,
            )
            for change in proposal.changes
        ],
        seq=1,
    )


async def distill_if_due(
    session: Session,
    provider: DistillProvider,
    config: ModelConfig,
) -> DistillEmit | None:
    """Distill when the session had a completed turn and memory changed.

    Does not write. ``None`` means skip the proposal event.
    """
    if completed_turn_count(session) < 1:
        return None
    result = await distill_session(
        session.workspace_path,
        turns_from_conversation(session),
        provider,
        config,
        session.cost_tracker,
    )
    if result is None:
        return None
    proposal_id = str(uuid.uuid4())
    return DistillEmit(
        proposal_id=proposal_id,
        proposal=result,
        event=proposal_event(session.id, proposal_id, result),
    )


async def apply_proposal(
    workspace: str | Path,
    proposal: DistillProposal,
    session: object,
) -> list[Path]:
    """Write accepted changes through the memory store, not ``fs_write``."""
    root = memory_dir(workspace)
    max_lines = memory_max_lines(session)
    written: list[Path] = []
    for change in proposal.changes:
        path = root / change.name
        if change.action == "delete":
            if await asyncio.to_thread(path.is_file):
                await asyncio.to_thread(path.unlink)
                written.append(path)
            continue
        await asyncio.to_thread(replace_memory_file, path, change.after or "", max_lines)
        written.append(path)
    return written
