"""Where "Schedule this chat" reads a session from (TD-3816).

``session_list`` carries a preset and a truncated title, not the engine
and not the first message. The title can also be renamed. This read is
the smallest one that still fills the job draft. It does not create a job.

The instruction is the earliest remaining user turn the session can still
show. A trimmed log has already dropped the original prefix, on disk and
in memory, so the earliest turn still in the window is the honest one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..protocol import DaemonEvent, UserTurn
from ..provider import ChatMessage
from .pin import EngineKind, normalize_engine

_EVENTS = "events.jsonl"
_CONVERSATION = "conversation.json"


class _ScheduleLog(Protocol):
    def events_from(self, seq: int) -> list[DaemonEvent]: ...


class LiveScheduleSession(Protocol):
    """The live session fields this read needs. Not the whole Session.

    Read-only on purpose. A writable attribute would have to be the same
    type as ``Session``'s, and the event log is its own class.
    """

    @property
    def workspace_path(self) -> str: ...

    @property
    def preset(self) -> str: ...

    @property
    def engine(self) -> Literal["native", "grok"]: ...

    @property
    def conversation(self) -> list[ChatMessage]: ...

    @property
    def event_log(self) -> _ScheduleLog: ...


@dataclass(frozen=True)
class SessionScheduleSource:
    """Draft fields copied from one chat. The instruction may be empty."""

    session_id: str
    workspace: str
    preset: str | None
    engine: EngineKind | None
    instruction: str


def session_schedule_source(
    data_dir: str | Path,
    session_id: str,
    *,
    stored_workspace: str | None,
    stored_preset: str,
    stored_engine: str | None,
    live: LiveScheduleSession | None,
) -> SessionScheduleSource | None:
    """None when this id is neither in memory nor in the session store.

    A live pin wins over the stored record: the session the user has open
    may have been repinned since the row was written. An engine the record
    does not have stays unset. It is not guessed as native.
    """
    if live is not None:
        workspace = live.workspace_path
    elif stored_workspace is not None and stored_workspace.strip():
        workspace = stored_workspace
    else:
        return None
    live_preset = live.preset.strip() if live is not None else ""
    preset = live_preset or stored_preset.strip() or None
    raw_engine = live.engine if live is not None else stored_engine
    try:
        engine = normalize_engine(raw_engine)
    except ValueError:
        engine = None
    return SessionScheduleSource(
        session_id=session_id,
        workspace=workspace,
        preset=preset,
        engine=engine,
        instruction=_instruction(Path(data_dir), session_id, live),
    )


def _instruction(
    data_dir: Path,
    session_id: str,
    live: LiveScheduleSession | None,
) -> str:
    if live is not None:
        text = _from_events(live.event_log.events_from(1))
        if text is not None:
            return text
        text = _from_messages(live.conversation)
        if text is not None:
            return text
    folder = _transcript_dir(data_dir, session_id)
    if folder is None:
        return ""
    text = _from_events_file(folder / _EVENTS)
    if text is not None:
        return text
    text = _from_conversation_file(folder / _CONVERSATION)
    return text if text is not None else ""


def _segment(session_id: str) -> bool:
    """One path segment, and not ``.`` or ``..``. Joining anything else escapes."""
    if session_id in {"", ".", ".."}:
        return False
    if "/" in session_id or "\\" in session_id or "\x00" in session_id:
        return False
    return Path(session_id).name == session_id


def _transcript_dir(data_dir: Path, session_id: str) -> Path | None:
    if not _segment(session_id):
        return None
    root = (data_dir / "sessions").resolve()
    folder = (root / session_id).resolve()
    if folder != root and root not in folder.parents:
        return None
    return folder


def _plain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _from_events(events: list[DaemonEvent]) -> str | None:
    for event in events:
        if isinstance(event, UserTurn):
            text = _plain(event.content)
            if text is not None:
                return text
    return None


def _from_messages(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if message.role != "user":
            continue
        text = _plain(message.content)
        if text is not None:
            return text
    return None


def _from_events_file(path: Path) -> str | None:
    """First non-blank ``user_turn`` in file order. Bad lines are skipped.

    The exception text of a bad line can quote the line, and the line can
    be a key, so nothing here is logged.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("type") != "user_turn":
            continue
        text = _plain(item.get("content"))
        if text is not None:
            return text
    return None


def _from_conversation_file(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        text = _plain(item.get("content"))
        if text is not None:
            return text
    return None
