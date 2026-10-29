"""On-disk session transcript and model conversation.

The registry (``sessions.json``) is only a list. Claude-like revive needs
the actual chat. This module writes what happened — events as they were
logged, conversation as the loop last had it — and refuses to invent
either on read. A session with no files here is an ``interrupted``
tombstone, not an empty thread pretending to be the old one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from .config import DEFAULT_LOG_MAX_EVENTS
from .logging import get_logger
from .protocol import DaemonEvent, HandshakeError, parse_daemon_event
from .provider import ChatMessage, FunctionCall, MessageContent, ToolCall

log = get_logger("tstd.session_persist")

_EVENTS = "events.jsonl"
_CONVERSATION = "conversation.json"
# Messages the daemon accepted that no turn has started yet (TD-5155).
_QUEUED = "queued.json"
# MCP tools ``tool_search`` loaded (TD-5127). The conversation alone loses
# them: compaction folds the calls that used them into a summary.
_MCP_TOOLS = "mcp_tools.json"
# Over the cap, the window is trimmed this fraction below it (TD-5193), so
# a long streamed reply rewrites the file once per that many events, not
# on every one. Caps under ten trim to exactly the cap.
_TRIM_SLACK_DIVISOR = 10


def _write_all(fd: int, text: str) -> None:
    """Write ``text`` fully to ``fd``, looping past partial ``os.write`` calls."""
    view = memoryview(text.encode("utf-8"))
    while view:
        view = view[os.write(fd, view) :]


def _replace_with(path: Path, text: str) -> None:
    """Swap ``text`` in for ``path`` through a temp file only this call uses.

    A shared temp name let one writer rename away another's file, so the
    second rename failed and that write was lost (TD-5016). ``mkstemp``
    creates the file 0600 in the same syscall: no world-readable instant.
    A failed write removes its own temp file before the error goes up.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            _write_all(fd, text)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass(frozen=True)
class AppendResult:
    """What the on-disk window looks like after one append."""

    earliest_seq: int
    trimmed: bool


@dataclass(frozen=True)
class _OnDisk:
    """What this writer last left in one session's events file (TD-5193).

    An append trusts these counts only while the file still has the
    ``stamp`` taken right after this writer's own last write; a file
    someone else changed, or one never seen, is read once to rebuild them.
    """

    stamp: tuple[int, int, int]
    count: int
    earliest_seq: int
    last_seq: int


def _stamp(st: os.stat_result) -> tuple[int, int, int]:
    """Which file, and how long: what an append or a rewrite by anyone changes.

    Not the modification time: some file systems settle it only when the
    handle closes, so it would look changed on every append there.
    """
    return (st.st_dev, st.st_ino, st.st_size)


def _stamp_of(path: Path) -> tuple[int, int, int]:
    """``_stamp`` through an open handle, as an append takes it (``fstat``, not ``stat``)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return _stamp(os.fstat(fd))
    finally:
        os.close(fd)


def _ends_mid_line(path: Path) -> bool:
    """True when the file's last byte is not a newline (a write cut short)."""
    try:
        with path.open("rb") as f:
            if f.seek(0, os.SEEK_END) == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


@dataclass(frozen=True)
class LoadedSession:
    """What disk actually had. ``conversation is None`` means no snapshot."""

    events: list[DaemonEvent]
    conversation: list[ChatMessage] | None


def chat_message_to_dict(message: ChatMessage) -> dict[str, Any]:
    """The OpenAI-shaped dict the provider already uses on the wire.

    Plus ``steer`` on a user message that joined a running turn (TD-5112),
    which the wire never carries, so a restored conversation still knows
    where its turns start, and ``check`` on a done-check's feedback
    (TD-5124), so it still knows which user messages the window numbers.
    """
    from .provider import ChatCompletionRequest

    row = ChatCompletionRequest._message_to_dict(message)
    if message.steer:
        row["steer"] = True
    if message.check:
        row["check"] = True
    return row


def _content_from_dict(content: object) -> MessageContent | None:
    """A string, or the text and image_url parts the save path writes.

    Attachments and screenshots are saved as a list of parts (TD-4705).
    Any other part type is still a load error, so nothing is guessed at.
    """
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("content must be a string or a list of parts")
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("content part is not an object")
        kind = part.get("type")
        if kind == "text":
            if not isinstance(part.get("text"), str):
                raise ValueError("text content part needs a string text")
        elif kind == "image_url":
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise ValueError("image_url content part needs a string url")
        else:
            raise ValueError(f"unknown content part type {kind!r}")
    return cast(list[dict[str, Any]], content)


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
    content = _content_from_dict(data.get("content"))
    tool_call_id = data.get("tool_call_id")
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        raise ValueError("tool_call_id must be a string")
    raw_details = data.get("reasoning_details")
    reasoning_details: list[dict[str, Any]] | None = None
    if raw_details is not None:
        if not isinstance(raw_details, list) or any(
            not isinstance(item, dict) for item in raw_details
        ):
            raise ValueError("reasoning_details must be a list of objects")
        reasoning_details = raw_details
    steer = data.get("steer", False)
    if not isinstance(steer, bool):
        raise ValueError("steer must be a boolean")
    check = data.get("check", False)
    if not isinstance(check, bool):
        raise ValueError("check must be a boolean")
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        reasoning_details=reasoning_details,
        steer=steer,
        check=check,
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
        self._trim_to = log_max_events - log_max_events // _TRIM_SLACK_DIVISOR
        self._write_lock = threading.Lock()
        # Per session, what the last append left on disk (TD-5193). Guarded
        # by the write lock; rebuilt from the file when missing or stale.
        self._on_disk: dict[str, _OnDisk] = {}

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

        An append in seq order under the cap is only a write (TD-5193): the
        counts kept from the last append say where the window stands. Every
        streamed delta is an event, so re-reading the whole file here held
        the GIL long enough to starve the loop reading the model's stream.
        The file is read and parsed only when this writer has not seen it
        as it is now (first append after a start, another writer), when an
        append lands out of seq order, or when the window must be trimmed;
        a trim goes a tenth below the cap so the next one is far off.
        """
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        events_path = path / _EVENTS
        line = event.model_dump_json() + "\n"
        with self._write_lock:
            known = self._on_disk.pop(session_id, None)
            # Created 0600 in the same syscall: no world-readable instant.
            fd = os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if known is not None and _stamp(os.fstat(fd)) != known.stamp:
                    known = None
                if known is None and _ends_mid_line(events_path):
                    # A write cut short (a crash) left half a line. It stays
                    # a line of its own, dropped as malformed on read, and
                    # does not swallow this event.
                    line = "\n" + line
                _write_all(fd, line)
                stamp = _stamp(os.fstat(fd))
            finally:
                os.close(fd)
            events_path.chmod(0o600)
            if (
                known is not None
                and event.seq > known.last_seq
                and known.count < self._log_max_events
            ):
                earliest = known.earliest_seq if known.count else event.seq
                self._on_disk[session_id] = _OnDisk(
                    stamp=stamp, count=known.count + 1, earliest_seq=earliest, last_seq=event.seq
                )
                return AppendResult(earliest_seq=earliest, trimmed=False)
            return self._settle_events(session_id, events_path, event.seq)

    def _settle_events(self, session_id: str, events_path: Path, appended: int) -> AppendResult:
        """Read the whole file, put it in seq order and inside the cap.

        The slow path of ``append_event``; the caller holds the write lock.
        The counts are kept for the next append only when the file is what
        was read and rewritten here: a failed rewrite leaves them unknown.
        """
        read = self._read_events(events_path)
        loaded = [] if read is None else read
        out_of_order = any(a.seq > b.seq for a, b in pairwise(loaded))
        if out_of_order:
            loaded.sort(key=lambda event: event.seq)
        kept = self._window(loaded)
        written = True
        if out_of_order or len(kept) < len(loaded):
            written = self._rewrite_events(events_path, kept)
        with contextlib.suppress(OSError):
            if written and read is not None:
                self._on_disk[session_id] = _OnDisk(
                    stamp=_stamp_of(events_path),
                    count=len(kept),
                    earliest_seq=kept[0].seq if kept else 0,
                    last_seq=kept[-1].seq if kept else 0,
                )
        earliest = kept[0].seq if kept else appended
        return AppendResult(earliest_seq=earliest, trimmed=len(kept) < len(loaded))

    def save_conversation(self, session_id: str, messages: list[ChatMessage]) -> None:
        """Replace the snapshot. Under the write lock, like every other writer."""
        payload = [chat_message_to_dict(m) for m in messages]
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            self._write_json(path / _CONVERSATION, payload)

    def save_queue(self, session_id: str, entries: list[dict[str, Any]]) -> None:
        """Replace the queued-message file; an empty queue removes it (TD-5155).

        Under the write lock, through a temp file and a rename, 0600 like
        the conversation.

        Raises:
            OSError: The file could not be written or removed. Unlike the
                other snapshots this is not only logged: the session decides,
                and a send it cannot save is refused (TD-5158).
        """
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            if entries:
                _replace_with(path / _QUEUED, json.dumps(entries))
                return
            (path / _QUEUED).unlink(missing_ok=True)

    def load_queue(self, session_id: str) -> list[dict[str, Any]]:
        """The queued-message rows on disk, or [] when there are none (TD-5155).

        Rows that are not objects are skipped; the caller checks fields.
        """
        path = self.dir_for(session_id) / _QUEUED
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "queued messages unreadable",
                extra={"extra_fields": {"session_id": session_id, "error": str(e)}},
            )
            return []
        if not isinstance(raw, list):
            log.warning(
                "queued messages are not a list", extra={"extra_fields": {"session_id": session_id}}
            )
            return []
        return [row for row in raw if isinstance(row, dict)]

    def save_loaded_tools(self, session_id: str, names: list[str]) -> None:
        """Replace the loaded-MCP-tool list; an empty list removes it (TD-5127)."""
        path = self.dir_for(session_id)
        path.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            if names:
                _replace_with(path / _MCP_TOOLS, json.dumps(names))
                return
            (path / _MCP_TOOLS).unlink(missing_ok=True)

    def load_loaded_tools(self, session_id: str) -> list[str]:
        """The loaded MCP tool names on disk, or [] (TD-5127).

        An unreadable file is logged and read as none: the model can
        search for the tools again.
        """
        path = self.dir_for(session_id) / _MCP_TOOLS
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # ValueError: bad JSON or not UTF-8
            log.warning(
                "loaded mcp tools unreadable",
                extra={"extra_fields": {"session_id": session_id, "error": str(e)}},
            )
            return []
        if not isinstance(raw, list):
            return []
        return [name for name in raw if isinstance(name, str)]

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
            # The next append reads the file again rather than trust counts
            # taken before this read could rewrite it.
            self._on_disk.pop(session_id, None)
            loaded = self._load_events(events_path)
            loaded.sort(key=lambda event: event.seq)
            events = self._window(loaded)
            if len(events) < len(loaded):
                self._rewrite_events(events_path, events)
        conversation = self._load_conversation(path / _CONVERSATION)
        return LoadedSession(events=events, conversation=conversation)

    def remove(self, session_id: str) -> None:
        path = self.dir_for(session_id)
        with self._write_lock:
            self._on_disk.pop(session_id, None)
        if path.is_dir():
            shutil.rmtree(path)

    def _load_events(self, path: Path) -> list[DaemonEvent]:
        events = self._read_events(path)
        return [] if events is None else events

    @staticmethod
    def _read_events(path: Path) -> list[DaemonEvent] | None:
        """The events in the file, malformed lines dropped; None if it cannot be read.

        Bytes that are not UTF-8 (a write cut short mid-character) spoil
        only their own line, which is then dropped as malformed.
        """
        if not path.is_file():
            return []
        events: list[DaemonEvent] = []
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as e:
            log.warning(
                "session events unreadable",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            return None
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
        return events

    def _window(self, events: list[DaemonEvent]) -> list[DaemonEvent]:
        """The events to keep: all of them up to the cap, else the newest ``_trim_to``."""
        if len(events) <= self._log_max_events:
            return events
        return events[-self._trim_to :]

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
    def _rewrite_events(path: Path, events: list[DaemonEvent]) -> bool:
        """Replace ``events.jsonl`` with ``events``. Unique temp + replace, 0o600.

        Returns False, after a warning, when the file could not be replaced.
        """
        try:
            _replace_with(path, "".join(event.model_dump_json() + "\n" for event in events))
            path.chmod(0o600)
        except OSError as e:
            log.warning(
                "failed to rotate session events",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
            return False
        return True

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        try:
            _replace_with(path, json.dumps(payload))
        except OSError as e:
            log.warning(
                "failed to write session snapshot",
                extra={"extra_fields": {"path": str(path), "error": str(e)}},
            )
