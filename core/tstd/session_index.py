"""Full-text index over what sessions said (TD-5129).

One SQLite file, ``session_index.db`` in the data dir, holds the text of
every user message and assistant reply the session logs carry, in an FTS5
table. It is fed from the same events ``events.jsonl`` gets, already
redacted by the session log, so it holds nothing the log does not and a
secret scrubbed there never lands here. Nothing in this module opens a
socket: the index, the search and the file stay on this computer
(prime directive 3).

**Updated as sessions write.** The daemon hands each logged event to
:meth:`SessionIndex.observe` right after it is appended to disk. A user
turn is a row. Streamed reply text is buffered per model response and
written as one row when the next non-text event arrives (a tool call, the
turn's end), so a reply costs one write, not one per token. A retracted
response (TD-5159) is dropped from the buffer and from the table.

**Turns.** Each row carries its turn: the 1-based number of the user
message that started it, counted in the session's own log, and that
message's ``turn_id``. A message sent mid-turn (TD-5112) belongs to the
turn it joined. A fork (``conversation_reset``) restarts the count at the
edited message, so the next turn has the number the window shows for it.

**Restart safety.** ``indexed_sessions`` records, per session, the last
seq whose effects are all in the table (never one inside a reply still
being buffered). On restore the daemon replays the log from there with
:meth:`SessionIndex.catch_up`, so a crash mid-reply loses nothing and a
session written before this index existed is indexed on first start.
Rows outlive the log window: an event trimmed from ``events.jsonl`` after
it was indexed stays searchable, which is the point of recall.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .logging import get_logger
from .protocol import (
    AssistantDelta,
    AssistantReasoning,
    AssistantRetract,
    ConversationReset,
    DaemonEvent,
    UserTurn,
)

log = get_logger("tstd.session_index")

INDEX_FILE = "session_index.db"
SCHEMA_VERSION = 1
#: Words a snippet shows around a match. FTS5 allows at most 64.
RAIL_SNIPPET_TOKENS = 12
RECALL_SNIPPET_TOKENS = 40
ELLIPSIS = "…"
#: Most query words used; a longer query is cut, not refused.
MAX_QUERY_TERMS = 12

Role = Literal["user", "assistant"]

# Letters and digits only: the unicode61 tokenizer splits on everything
# else (``date_parser`` is two words there), so the query must too.
_TERM = re.compile(r"[^\W_]+", re.UNICODE)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS indexed_sessions (
    session_id TEXT PRIMARY KEY,
    indexed_seq INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    turn_id TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    turn_id TEXT,
    role TEXT NOT NULL,
    response_id TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, response_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
"""


class IndexUnavailable(Exception):
    """The index could not be opened (no FTS5, an unreadable file, a newer schema)."""


@dataclass(frozen=True)
class SearchHit:
    """One matching message, best first in a result list."""

    session_id: str
    turn: int
    turn_id: str | None
    role: Role
    snippet: str


@dataclass(frozen=True)
class RecallHit:
    """A hit the ``session_search`` tool shows: a :class:`SearchHit` plus the
    session's title and when it was last used, from the session registry."""

    session_id: str
    title: str | None
    updated_at: str
    turn: int
    turn_id: str | None
    role: Role
    snippet: str


@dataclass
class _Pending:
    seq: int
    response_id: str | None
    chunks: list[str] = field(default_factory=list)


@dataclass
class _Live:
    """What the index knows of one session between writes."""

    seen_seq: int
    committed_seq: int
    turn: int
    turn_id: str | None
    pending: list[_Pending] = field(default_factory=list)


@dataclass
class _Ops:
    rows: list[tuple[int, int, str | None, Role, str | None, str]] = field(default_factory=list)
    retract: list[str] = field(default_factory=list)
    state_changed: bool = False

    def __bool__(self) -> bool:
        return bool(self.rows or self.retract or self.state_changed)


def match_expression(query: str, *, any_term: bool = False) -> str | None:
    """An FTS5 MATCH string for *query*, or None when it has no words.

    Each word is quoted, so nothing the person or the model types is read
    as FTS syntax (``OR``, ``NEAR``, a column filter, a stray quote), and
    each is a prefix: ``pars`` finds ``parser`` as the rail is typed.
    Words are ANDed; *any_term* ORs them for the fallback search.
    """
    terms = list(dict.fromkeys(t.lower() for t in _TERM.findall(query)))[:MAX_QUERY_TERMS]
    if not terms:
        return None
    joiner = " OR " if any_term else " "
    return joiner.join(f'"{term}"*' for term in terms)


class SessionIndex:
    """The FTS index file and the per-session state that feeds it.

    Thread-safe, and the daemon calls it from ``asyncio.to_thread``. Writes
    take one lock; a search reads on a connection of its own under another,
    so ranking a big index never holds up the events every session logs
    (WAL lets a reader and the writer run at once). The file opens on first
    use, so a daemon that never logs or searches never creates it.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / INDEX_FILE
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._read_lock = threading.Lock()
        self._reader: sqlite3.Connection | None = None
        self._failed: str | None = None
        self._live: dict[str, _Live] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        """False once opening failed; the reason was logged then."""
        with self._lock:
            try:
                self._db()
            except IndexUnavailable:
                return False
            return True

    def close(self) -> None:
        with self._read_lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── Writing ──────────────────────────────────────────────────────

    def observe(self, event: DaemonEvent) -> None:
        """Take one logged event, in seq order for its session.

        Never raises for a database problem: the session's log is the
        record and the index only a view of it, so a failed write is
        logged, the session's state is dropped, and the next restore's
        catch-up fills the gap from disk.
        """
        session_id = getattr(event, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return
        with self._lock:
            try:
                conn = self._db()
                live = self._live_for(conn, session_id)
                ops = self._apply(live, event)
                if ops:
                    with conn:
                        self._write(conn, session_id, live, ops)
            except IndexUnavailable:
                return
            except sqlite3.Error as e:
                self._live.pop(session_id, None)
                log.warning(
                    "session index write failed",
                    extra={"extra_fields": {"session_id": session_id, "error": str(e)}},
                )

    def catch_up(self, session_id: str, events: Iterable[DaemonEvent]) -> int:
        """Index the logged events this index has not seen yet; one transaction.

        Called on restore with the events read from disk, before the
        session logs anything new. Returns how many events were applied.
        """
        applied = 0
        with self._lock:
            try:
                conn = self._db()
                live = self._live_for(conn, session_id)
                with conn:
                    for event in sorted(events, key=lambda e: e.seq):
                        if getattr(event, "session_id", session_id) != session_id:
                            continue
                        if event.seq <= live.seen_seq:
                            continue
                        ops = self._apply(live, event)
                        applied += 1
                        if ops:
                            self._write(conn, session_id, live, ops)
                    # A reply the log ends inside cannot go on after a
                    # restart (revive seals the turn), so it is written now.
                    if live.pending:
                        ops = _Ops()
                        self._flush_pending(live, ops)
                        self._write(conn, session_id, live, ops)
            except IndexUnavailable:
                return 0
            except sqlite3.Error as e:
                self._live.pop(session_id, None)
                log.warning(
                    "session index catch-up failed",
                    extra={"extra_fields": {"session_id": session_id, "error": str(e)}},
                )
                return 0
        return applied

    def flush(self, session_id: str | None = None) -> None:
        """Write buffered reply text now (all sessions when None)."""
        with self._lock:
            try:
                conn = self._db()
            except IndexUnavailable:
                return
            ids = [session_id] if session_id is not None else list(self._live)
            for sid in ids:
                live = self._live.get(sid)
                if live is None or not live.pending:
                    continue
                ops = _Ops()
                self._flush_pending(live, ops)
                try:
                    with conn:
                        self._write(conn, sid, live, ops)
                except sqlite3.Error as e:
                    self._live.pop(sid, None)
                    log.warning(
                        "session index flush failed",
                        extra={"extra_fields": {"session_id": sid, "error": str(e)}},
                    )

    def remove(self, session_id: str) -> None:
        """Forget a deleted session: its rows and its state."""
        with self._lock:
            self._live.pop(session_id, None)
            try:
                conn = self._db()
                with conn:
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM indexed_sessions WHERE session_id = ?", (session_id,))
            except IndexUnavailable:
                return
            except sqlite3.Error as e:
                log.warning(
                    "session index remove failed",
                    extra={"extra_fields": {"session_id": session_id, "error": str(e)}},
                )

    # ── Searching ────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        session_ids: Collection[str] | None = None,
        limit: int = 20,
        per_session: int = 1,
        snippet_tokens: int = RAIL_SNIPPET_TOKENS,
    ) -> list[SearchHit]:
        """The best messages for *query*, at most *per_session* per session.

        Every word must match (as a prefix); when that finds nothing, any
        word may, ranked by BM25 so rare words count most. *session_ids*
        narrows the search to those sessions (an empty collection finds
        nothing). Raises :class:`IndexUnavailable` when the index is off,
        or when this search failed in SQLite (the next one may not).
        """
        limit = max(limit, 1)
        per_session = max(per_session, 1)
        tokens = min(max(snippet_tokens, 1), 64)
        with self._lock:
            # Opened (and migrated) first, so an index that is off says so
            # even when there is nothing to search.
            self._db()
        if session_ids is not None and not session_ids:
            return []
        with self._read_lock:
            try:
                conn = self._read_db()
                for any_term in (False, True):
                    expression = match_expression(query, any_term=any_term)
                    if expression is None:
                        return []
                    hits = self._ranked(conn, expression, session_ids, limit, per_session, tokens)
                    if hits:
                        return hits
            except sqlite3.Error as e:
                log.warning(
                    "session index search failed",
                    extra={"extra_fields": {"error": str(e)}},
                )
                raise IndexUnavailable(str(e) or type(e).__name__) from e
        return []

    def _ranked(
        self,
        conn: sqlite3.Connection,
        expression: str,
        session_ids: Collection[str] | None,
        limit: int,
        per_session: int,
        tokens: int,
    ) -> list[SearchHit]:
        # Rank every match, keep the best *per_session* of each session
        # inside SQL, then take the overall best *limit*. Capping before
        # the LIMIT means one chatty session cannot crowd the others out;
        # snippets are cut only for the rows that are kept.
        scope = ""
        params: list[object] = [expression]
        if session_ids is not None:
            scope = " AND m.session_id IN (SELECT value FROM json_each(?))"
            params.append(_json_list(session_ids))
        sql = (
            # bm25() only works in the FTS query itself, so the score is
            # materialised first and the window ranks over that column.
            "WITH scored AS MATERIALIZED ("
            " SELECT m.id AS id, m.session_id AS session_id, m.seq AS seq,"
            " bm25(messages_fts) AS score"
            " FROM messages_fts JOIN messages AS m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ?" + scope + "),"
            " ranked AS (SELECT id, seq, score, ROW_NUMBER() OVER ("
            "PARTITION BY session_id ORDER BY score, seq DESC) AS n FROM scored),"
            " kept AS (SELECT id, seq, score FROM ranked WHERE n <= ?"
            " ORDER BY score, seq DESC LIMIT ?)"
            " SELECT m.session_id, m.turn, m.turn_id, m.role,"
            " snippet(messages_fts, 0, '', '', ?, ?)"
            " FROM kept JOIN messages_fts ON messages_fts.rowid = kept.id"
            " JOIN messages AS m ON m.id = kept.id"
            " WHERE messages_fts MATCH ?"
            " ORDER BY kept.score, kept.seq DESC"
        )
        params += [per_session, limit, ELLIPSIS, tokens, expression]
        return [
            SearchHit(
                session_id=session_id,
                turn=int(turn),
                turn_id=turn_id,
                role="user" if role == "user" else "assistant",
                snippet=" ".join(str(snippet).split()),
            )
            for session_id, turn, turn_id, role, snippet in conn.execute(sql, params)
        ]

    # ── Internals ────────────────────────────────────────────────────

    def _db(self) -> sqlite3.Connection:
        """The open connection; opens and migrates on first use."""
        if self._conn is not None:
            return self._conn
        if self._failed is not None:
            raise IndexUnavailable(self._failed)
        try:
            self._conn = self._open()
        except (sqlite3.Error, OSError, IndexUnavailable) as e:
            self._failed = str(e) or type(e).__name__
            log.warning(
                "session index unavailable; search is off",
                extra={"extra_fields": {"path": str(self._path), "error": self._failed}},
            )
            raise IndexUnavailable(self._failed) from e
        return self._conn

    def _read_db(self) -> sqlite3.Connection:
        """The search connection; opened after :meth:`_db` made the file."""
        if self._reader is None:
            reader = sqlite3.connect(self._path, check_same_thread=False)
            try:
                reader.execute("PRAGMA query_only=ON")
            except BaseException:
                reader.close()
                raise
            self._reader = reader
        return self._reader

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Created 0600 before SQLite opens it; the -wal and -shm files
        # SQLite makes beside it copy the main file's mode.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # The log on disk is the record; a power cut that loses the
            # last commits is repaired by the next catch-up.
            conn.execute("PRAGMA synchronous=NORMAL")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if row is not None:
                found = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if found is not None and int(found[0]) > SCHEMA_VERSION:
                    raise IndexUnavailable(
                        f"index schema {found[0]} is newer than this TST Desk ({SCHEMA_VERSION})"
                    )
            conn.executescript(f"BEGIN;{_SCHEMA}COMMIT;")
        except BaseException:
            conn.close()
            raise
        return conn

    def _live_for(self, conn: sqlite3.Connection, session_id: str) -> _Live:
        live = self._live.get(session_id)
        if live is not None:
            return live
        row = conn.execute(
            "SELECT indexed_seq, turn, turn_id FROM indexed_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            live = _Live(seen_seq=0, committed_seq=0, turn=0, turn_id=None)
        else:
            seq, turn, turn_id = int(row[0]), int(row[1]), row[2]
            live = _Live(seen_seq=seq, committed_seq=seq, turn=turn, turn_id=turn_id)
        self._live[session_id] = live
        return live

    def _apply(self, live: _Live, event: DaemonEvent) -> _Ops:
        """Fold *event* into *live*; return what must be written."""
        ops = _Ops()
        if event.seq <= live.seen_seq:
            return ops
        live.seen_seq = event.seq
        if isinstance(event, AssistantDelta):
            if event.delta:
                last = live.pending[-1] if live.pending else None
                if last is None or last.response_id != event.response_id:
                    last = _Pending(seq=event.seq, response_id=event.response_id)
                    live.pending.append(last)
                last.chunks.append(event.delta)
            return ops
        if isinstance(event, AssistantReasoning):
            # Thinking is not what was said; it neither counts nor ends a reply.
            return ops
        if isinstance(event, AssistantRetract):
            live.pending = [p for p in live.pending if p.response_id != event.response_id]
            ops.retract.append(event.response_id)
        self._flush_pending(live, ops)
        if isinstance(event, UserTurn):
            turn_id = event.turn_id
            if event.steer is None:
                live.turn += 1
                live.turn_id = event.turn_id
                ops.state_changed = True
            else:
                # Sent mid-turn (TD-5112): part of the turn it joined.
                turn_id = event.steer.turn_id
                if live.turn_id is None:
                    live.turn_id = turn_id
                    ops.state_changed = True
            if event.content.strip():
                ops.rows.append((event.seq, live.turn, turn_id, "user", None, event.content))
        elif isinstance(event, ConversationReset):
            if event.user_turns is not None:
                # A sibling was selected: no user_turn follows, and the next
                # one sent comes after every turn the sibling holds.
                live.turn = max(event.user_turns, 0)
            else:
                # The edited message is turn user_index + 1; its user_turn follows.
                live.turn = max(event.user_index, 0)
            live.turn_id = None
            ops.state_changed = True
        return ops

    @staticmethod
    def _flush_pending(live: _Live, ops: _Ops) -> None:
        for pending in live.pending:
            text = "".join(pending.chunks)
            if text.strip():
                ops.rows.append(
                    (pending.seq, live.turn, live.turn_id, "assistant", pending.response_id, text)
                )
        live.pending = []

    @staticmethod
    def _write(conn: sqlite3.Connection, session_id: str, live: _Live, ops: _Ops) -> None:
        for response_id in ops.retract:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND response_id = ?",
                (session_id, response_id),
            )
        conn.executemany(
            "INSERT INTO messages(session_id, seq, turn, turn_id, role, response_id, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(session_id, *row) for row in ops.rows],
        )
        if not live.pending:
            live.committed_seq = live.seen_seq
        conn.execute(
            "INSERT INTO indexed_sessions(session_id, indexed_seq, turn, turn_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "indexed_seq = excluded.indexed_seq, turn = excluded.turn, "
            "turn_id = excluded.turn_id",
            (session_id, live.committed_seq, live.turn, live.turn_id),
        )


def _json_list(values: Collection[str]) -> str:
    import json

    return json.dumps(sorted(values))
