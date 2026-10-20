"""TD-5129: the local full-text index over what sessions said.

Fed the same events ``events.jsonl`` gets; searched by the rail and by
``session_search``. These drive :class:`SessionIndex` directly with the
events a turn logs, then a fresh instance over the same file, the way a
restart sees it.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tstd.protocol import (
    AssistantDelta,
    AssistantReasoning,
    AssistantRetract,
    ConversationReset,
    DaemonEvent,
    SteerPoint,
    ToolCall,
    TurnComplete,
    UserTurn,
)
from tstd.session_index import INDEX_FILE, SessionIndex, match_expression

SID = "sess-a"


class Log:
    """Stamps seqs the way a session's event log does."""

    def __init__(self, session_id: str = SID) -> None:
        self.session_id = session_id
        self.seq = 0
        self.events: list[DaemonEvent] = []

    def _add(self, event: DaemonEvent) -> DaemonEvent:
        self.seq += 1
        event.seq = self.seq
        self.events.append(event)
        return event

    def user(self, text: str, turn_id: str, steer: str | None = None) -> DaemonEvent:
        point = SteerPoint(turn_id=steer, step=1) if steer else None
        return self._add(
            UserTurn(session_id=self.session_id, turn_id=turn_id, content=text, steer=point, seq=1)
        )

    def says(self, text: str, response_id: str = "r1") -> list[DaemonEvent]:
        return [
            self._add(
                AssistantDelta(
                    session_id=self.session_id, delta=f"{word} ", response_id=response_id, seq=1
                )
            )
            for word in text.split(" ")
        ]

    def thinks(self, text: str) -> DaemonEvent:
        return self._add(AssistantReasoning(session_id=self.session_id, delta=text, seq=1))

    def tool(self, name: str = "fs_read") -> DaemonEvent:
        return self._add(
            ToolCall(
                session_id=self.session_id,
                tool_call_id=f"c{self.seq}",
                name=name,
                arguments={"path": "secret_tool_arg.py"},
                seq=1,
            )
        )

    def done(self) -> DaemonEvent:
        return self._add(
            TurnComplete(
                session_id=self.session_id, tokens=1, cost=0.0, tier="brain", duration=0.1, seq=1
            )
        )

    def retract(self, response_id: str) -> DaemonEvent:
        return self._add(
            AssistantRetract(
                session_id=self.session_id, response_id=response_id, reason="stream_stalled", seq=1
            )
        )

    def reset(self, user_index: int, content: str) -> DaemonEvent:
        return self._add(
            ConversationReset(
                session_id=self.session_id,
                user_index=user_index,
                sibling_index=1,
                sibling_count=2,
                content=content,
                seq=1,
            )
        )


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SessionIndex]:
    made = SessionIndex(tmp_path)
    yield made
    made.close()


def _feed(index: SessionIndex, events: list[Any]) -> None:
    for event in events:
        for one in event if isinstance(event, list) else [event]:
            index.observe(one)


def _two_turns(log: Log) -> list[Any]:
    return [
        log.user("Why does the date parser drop the timezone?", "t1"),
        log.thinks("hidden chain of thought about quokkas"),
        log.says("Let me read it.", "r1"),
        log.tool(),
        log.says("The parser strips the offset before ISO weeks are read.", "r2"),
        log.done(),
        log.user("Fix it with zoneinfo please", "t2"),
        log.says("Done: zoneinfo keeps the offset now.", "r3"),
        log.done(),
    ]


def test_user_and_assistant_text_is_found_with_its_turn(index: SessionIndex) -> None:
    log = Log()
    _feed(index, _two_turns(log))
    [hit] = index.search("timezone", per_session=3)
    assert (hit.session_id, hit.turn, hit.turn_id, hit.role) == (SID, 1, "t1", "user")
    [hit] = index.search("offset ISO", per_session=3)
    assert (hit.turn, hit.role) == (1, "assistant")
    assert "strips the offset" in hit.snippet
    hits = index.search("zoneinfo", per_session=3)
    assert {(h.turn, h.role) for h in hits} == {(2, "user"), (2, "assistant")}


def test_thinking_and_tool_arguments_are_not_indexed(index: SessionIndex) -> None:
    _feed(index, _two_turns(Log()))
    assert index.search("quokkas") == []
    assert index.search("secret_tool_arg") == []


def test_a_reply_is_written_once_when_it_ends_not_per_token(index: SessionIndex) -> None:
    log = Log()
    _feed(index, [log.user("hello", "t1"), log.says("one two three", "r1")])
    # Still streaming: buffered, not yet searchable.
    assert index.search("three") == []
    index.observe(log.done())
    [hit] = index.search("three")
    assert hit.snippet == "one two three"


def test_words_are_prefixes_and_fts_syntax_is_plain_text(index: SessionIndex) -> None:
    _feed(index, _two_turns(Log()))
    assert index.search("pars")  # typed so far, finds "parser"
    for hostile in ('"', "NEAR(a b)", "text:parser", "parser OR", "*", "-zone", "AND"):
        index.search(hostile)  # never raises an FTS syntax error
    assert match_expression("text:parser OR") == '"text"* "parser"* "or"*'
    assert match_expression("  ... ") is None


def test_any_word_is_the_fallback_when_all_words_find_nothing(index: SessionIndex) -> None:
    _feed(index, _two_turns(Log()))
    [hit] = index.search("zoneinfo banana")
    assert hit.turn == 2


def test_a_withdrawn_reply_leaves_no_row(index: SessionIndex) -> None:
    log = Log()
    _feed(
        index,
        [
            log.user("go", "t1"),
            log.says("half finished stalled answer", "r1"),
            log.retract("r1"),
            log.says("the real answer", "r2"),
            log.done(),
        ],
    )
    assert index.search("stalled") == []
    assert index.search("real answer")


def test_a_mid_turn_message_belongs_to_the_turn_it_joined(index: SessionIndex) -> None:
    log = Log()
    _feed(
        index,
        [
            log.user("start the build", "t1"),
            log.tool("shell"),
            log.user("also run the linter", "t1b", steer="t1"),
            log.done(),
            log.user("thanks", "t2"),
            log.done(),
        ],
    )
    [hit] = index.search("linter")
    assert (hit.turn, hit.turn_id) == (1, "t1")
    [hit] = index.search("thanks")
    assert hit.turn == 2


def test_a_fork_numbers_the_edited_turn_as_the_window_does(index: SessionIndex) -> None:
    log = Log()
    _feed(index, _two_turns(log))
    _feed(
        index,
        [log.reset(1, "Fix it with dateutil"), log.user("Fix it with dateutil", "t2b"), log.done()],
    )
    [hit] = index.search("dateutil")
    assert (hit.turn, hit.turn_id) == (2, "t2b")


def test_scoping_and_one_hit_per_session(index: SessionIndex) -> None:
    a, b = Log("a"), Log("b")
    _feed(index, _two_turns(a) + _two_turns(b))
    assert {h.session_id for h in index.search("parser")} == {"a", "b"}
    assert [h.session_id for h in index.search("parser", session_ids={"b"})] == ["b"]
    assert index.search("parser", session_ids=set()) == []
    assert len(index.search("parser", per_session=5, session_ids={"a"})) > 1


def test_removing_a_session_drops_its_rows(index: SessionIndex) -> None:
    a, b = Log("a"), Log("b")
    _feed(index, _two_turns(a) + _two_turns(b))
    index.remove("a")
    assert {h.session_id for h in index.search("parser")} == {"b"}


def test_restart_catches_up_from_the_log_without_duplicates(tmp_path: Path) -> None:
    log = Log()
    events = _two_turns(log)
    first = SessionIndex(tmp_path)
    # The first process saw only the first turn, and died mid-reply.
    _feed(first, events[:5])
    first.close()

    second = SessionIndex(tmp_path)
    try:
        second.catch_up(SID, log.events)
        second.catch_up(SID, log.events)  # idempotent
        assert len(second.search("strips", per_session=9)) == 1
        assert len(second.search("timezone", per_session=9)) == 1
        [hit] = second.search("zoneinfo keeps")
        assert hit.turn == 2
        # Live writing goes on from where the log left off.
        _feed(second, [log.user("and the leap second", "t3"), log.done()])
        [hit] = second.search("leap")
        assert hit.turn == 3
    finally:
        second.close()


def test_a_session_older_than_the_index_is_indexed_on_first_start(tmp_path: Path) -> None:
    log = Log()
    _two_turns(log)
    index = SessionIndex(tmp_path)
    try:
        assert index.catch_up(SID, log.events) == len(log.events)
        assert index.search("timezone")
    finally:
        index.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_index_file_is_private(index: SessionIndex, tmp_path: Path) -> None:
    _feed(index, _two_turns(Log()))
    mode = stat.S_IMODE(os.stat(tmp_path / INDEX_FILE).st_mode)
    assert mode == 0o600


def test_nothing_leaves_the_machine(index: SessionIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    """Directive 3: indexing and searching open no socket at all."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("session index opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    _feed(index, _two_turns(Log()))
    assert index.search("parser")


def test_an_unopenable_index_turns_search_off_without_breaking_writes(tmp_path: Path) -> None:
    (tmp_path / INDEX_FILE).mkdir()  # a directory where the file should be
    index = SessionIndex(tmp_path)
    _feed(index, _two_turns(Log()))  # logs a warning, raises nothing
    assert index.available is False
    from tstd.session_index import IndexUnavailable

    with pytest.raises(IndexUnavailable):
        index.search("parser")
