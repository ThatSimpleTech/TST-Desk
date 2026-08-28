"""On-disk session transcript and model conversation.

The registry (``sessions.json``) is only a list. Claude-like revive needs
the actual chat. This module writes what happened — events as they were
logged, conversation as the loop last had it — and refuses to invent
either on read. A session with no files here is an ``interrupted``
tombstone, not an empty thread pretending to be the old one.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .config import DEFAULT_LOG_MAX_EVENTS
from .logging import get_logger
from .protocol import DaemonEvent, HandshakeError, parse_daemon_event
from .provider import ChatMessage, FunctionCall, ToolCall

log = get_logger("tstd.session_persist")

_EVENTS = "events.jsonl"
_CONVERSATION = "conversation.json"


def _write_all(fd: int, text: str) -> None:
    """Write ``text`` fully to ``fd``, looping past partial ``os.write`` calls."""
    view = memoryview(text.encode("utf-8"))
    while view:
        view = view[os.write(fd, view) :]


@dataclass(frozen=True)
class AppendResult:
    """What the on-disk window looks like after one append."""

    earliest_seq: int
    trimmed: bool


@dataclass(frozen=True)
class LoadedSession:
    """What disk actually had. ``conversation is None`` means no snapshot."""

    events: list[DaemonEvent]
    conversation: list[ChatMessage] | None


def chat_message_to_dict(message: ChatMessage) -> dict[str, Any]:
    """The OpenAI-shaped dict the provider already uses on the wire."""
    from .provider import ChatCompletionRequest

    return ChatCompletionRequest._message_to_dict(message)


def chat_message_from_dict(data: dict[str, Any]) -> ChatMessage:
    """Rebuild a conversation row. Missing or extra keys are a load error."""
    if not isinstance(data, dict) or "role" not in data:
        raise ValueError("conversation row is not a chat message")
    raw_calls = data.get("tool_calls")
    tool_calls: list[ToolCall] | None = None
    if raw_calls:
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls must be a list")
        tool_calls = []
        for item in raw_calls:
            if not isinstance(item, dict):
                raise ValueError("tool_call is not an object")
            fn = item.get("function")
            if not isinstance(fn, dict):
                raise ValueError("tool_call function is missing")
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id", "")),
                    function=FunctionCall(
                        name=str(fn.get("name", "")),
                        arguments=str(fn.get("arguments", "")),
                    ),
                )
            )
    role = data["role"]
    if role not in ("system", "user", "assistant", "tool"):
        raise ValueError(f"unknown chat role {role!r}")
    content = data.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("content must be a string")
    tool_call_id = data.get("tool_call_id")
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        raise ValueError("tool_call_id must be a string")
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


class SessionPersist:
    """One directory per session under ``data_dir/sessions/<id>/``."""

    def __init__(
        self,
        data_dir: Path,
        *,
        log_max_events: int | None = None,
    ) -> None:
        self._root = data_dir / "sessions"
        # Zero/None is a missing cap, not "keep forever".
        if log_max_events is None or log_max_events < 1:
            log_max_events = DEFAULT_LOG_MAX_EVENTS
        self._log_max_events = log_max_events
        self._write_lock = threading.Lock()

    def dir_for(self, session_id: str) -> Path:
        return self._root / session_id

    def prepare(self, session_id: str) -> None:
        """Create the directory and an empty conversation snapshot.

        New sessions get a snapshot immediately so a crash before the first
        turn still has something honest to restore: no messages, not a
        fabricated history.
        """
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        convo = path / _CONVERSATION
        if not convo.exists():
            self._write_json(convo, [])

    def append_event(self, session_id: str, event: DaemonEvent) -> AppendResult:
        """Append one already-redacted event and keep the file inside the cap.

        Sync: the caller is in ``to_thread``. The new line is written first
        so a seq that reached this call is on disk before rotation. Over
        cap, oldest lines drop and the file is rewritten atomically.
        """
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        events_path = path / _EVENTS
        line = event.model_dump_json() + "\n"
        with self._write_lock:
            # Created 0600 in the same syscall: no world-readable instant.
            fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                _write_all(fd, line)
            finally:
                os.close(fd)
            events_path.chmod(0o600)
            loaded = self._load_events(events_path)
            kept = self._apply_window(events_path, loaded)
            earliest = kept[0].seq if kept else event.seq
            return AppendResult(earliest_seq=earliest, trimmed=len(kept) < len(loaded))

    def save_conversation(self, session_id: str, messages: list[ChatMessage]) -> None:
        payload = [chat_message_to_dict(m) for m in messages]
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        self._write_json(path / _CONVERSATION, payload)

    def load(self, session_id: str) -> LoadedSession | None:
        """Read what is on disk. ``None`` if this session has no directory.

        An already-oversized file is windowed here, not only on the next
        append, so restore and attach see the same cap as a live write.
        """
        path = self.dir_for(session_id)
        if not path.is_dir():
            return None
        events_path = path / _EVENTS
        with self._write_lock:
            events = self._apply_window(events_path, self._load_events(events_path))
        conversation = self._load_conversation(path / _CONVERSATION)
        return LoadedSession(events=events, conversation=conversation)

    def remove(self, session_id: str) -> None:
        path = self.dir_for(session_id)
        if path.is_dir():
            shutil.rmtree(path)

    def _load_events(self, path: Path) -> list[DaemonEvent]:
        if not path.is_file():
            return []
        events: list[DaemonEvent] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning(
                "session events unreadable",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            return []
        for i, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(cast(DaemonEvent, parse_daemon_event(line)))
            except (HandshakeError, ValueError) as e:
                log.warning(
                    "dropping malformed session event",
                    extra={"extra_fields": {"path": str(path), "line": i, "error": str(e)}},
                )
        events.sort(key=lambda event: event.seq)
        return events

    def _apply_window(self, path: Path, events: list[DaemonEvent]) -> list[DaemonEvent]:
        """Keep the newest ``log_max_events``. Rewrites the file when over cap."""
        if len(events) <= self._log_max_events:
            return events
        kept = events[-self._log_max_events :]
        self._rewrite_events(path, kept)
        return kept

    def _load_conversation(self, path: Path) -> list[ChatMessage] | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "session conversation unreadable",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            return None
        if not isinstance(raw, list):
            log.warning("session conversation is not a list")
            return None
        try:
            return [chat_message_from_dict(row) for row in raw]
        except (TypeError, ValueError) as e:
            log.warning(
                "session conversation malformed",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            return None

    @staticmethod
    def _rewrite_events(path: Path, events: list[DaemonEvent]) -> None:
        """Replace ``events.jsonl`` with ``events``. Temp + replace, mode 0o600."""
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            # Created 0600 in the same syscall: no world-readable instant.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                _write_all(fd, "".join(event.model_dump_json() + "\n" for event in events))
            finally:
                os.close(fd)
            tmp.chmod(0o600)
            os.replace(tmp, path)
            path.chmod(0o600)
        except OSError as e:
            log.warning(
                "failed to rotate session events",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            # Created 0600 in the same syscall: no world-readable instant.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                _write_all(fd, json.dumps(payload))
            finally:
                os.close(fd)
            tmp.chmod(0o600)
            os.replace(tmp, path)
        except OSError as e:
            log.warning(
                "failed to write session snapshot",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            if tmp.exists():
                tmp.unlink(missing_ok=True)
