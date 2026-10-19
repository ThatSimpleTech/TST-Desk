"""Context gauge and compact on request (TD-5109).

The loop reports how full the window is before each model call; the
window can compact now (``/compact``, with optional guidance) or from a
chosen user turn ("Summarize from here").  Guidance is deterministic: it
heads the summary and keeps more of the messages that mention it.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_context_fit import _forge_config, _run, _tier
from tests.test_loop import wait_for_turn
from tstd.compaction import (
    _SUMMARY_CHARS_PER_MESSAGE,
    _SUMMARY_HEADER,
    ContextMeter,
    budget_threshold,
    compact_on_request,
)
from tstd.context.tokens import HeuristicTokenCounter
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted, ContextUsage
from tstd.provider import ChatMessage, content_as_text
from tstd.session import Session
from tstd.session_persist import chat_message_from_dict, chat_message_to_dict

_COUNTER = HeuristicTokenCounter()


def _msg(role: str, text: str) -> ChatMessage:
    return ChatMessage(role=role, content=text)  # type: ignore[arg-type]


def _conversation(turns: int = 3) -> list[ChatMessage]:
    messages = [_msg("system", "You are helpful.")]
    for n in range(turns):
        messages.append(_msg("user", f"question {n}"))
        messages.append(_msg("assistant", f"answer {n}"))
    return messages


def _texts(messages: list[ChatMessage]) -> list[str]:
    return [content_as_text(m.content) for m in messages]


def _meter() -> ContextMeter:
    return ContextMeter("brain", _tier(), _COUNTER, None)


def _session(tmp_path: Path, turns: int = 3) -> Session:
    session = Session(str(tmp_path))
    session.conversation.extend(_conversation(turns))
    session.context_meter = _meter()
    return session


def _events(session: Session, kind: type) -> list:
    return [e for e in session.event_log.all_events if isinstance(e, kind)]


# ── compact_on_request ───────────────────────────────────────────────────


class TestCompactOnRequest:
    def test_manual_keeps_the_last_user_turn(self) -> None:
        result = compact_on_request(_conversation(3), _COUNTER)
        assert result is not None
        compacted, stats = result
        assert compacted[0].content == "You are helpful."
        assert compacted[1].role == "system"
        summary = content_as_text(compacted[1].content)
        assert summary.startswith(_SUMMARY_HEADER)
        assert "User: question 0" in summary and "User: question 1" in summary
        assert _texts(compacted[2:]) == ["question 2", "answer 2"]
        assert stats.dropped_messages == 4
        assert stats.dropped_user_turns == 2
        assert stats.tokens_after < stats.tokens_before or stats.tokens_before < 50

    def test_from_here_keeps_earlier_turns_verbatim(self) -> None:
        messages = _conversation(3)
        result = compact_on_request(messages, _COUNTER, from_position=3)  # "question 1"
        assert result is not None
        compacted, stats = result
        assert _texts(compacted[:3]) == ["You are helpful.", "question 0", "answer 0"]
        assert len(compacted) == 4
        summary = content_as_text(compacted[3].content)
        assert "User: question 1" in summary and "Assistant: answer 2" in summary
        assert stats.dropped_user_turns == 2

    def test_guidance_heads_the_summary_and_keeps_more_of_what_it_names(self) -> None:
        long_on = "The parser bug is in tokenize. " + "detail " * 120
        long_off = "Unrelated chatter about lunch. " + "filler " * 120
        messages = [
            _msg("system", "sys"),
            _msg("user", long_on),
            _msg("assistant", long_off),
            _msg("user", "latest"),
        ]
        result = compact_on_request(messages, _COUNTER, guidance="keep the parser work")
        assert result is not None
        summary = content_as_text(result[0][1].content)
        lines = summary.splitlines()
        assert lines[0] == f"{_SUMMARY_HEADER} (1 user turn)"
        assert lines[1] == "Kept in view, as asked: keep the parser work"
        on_topic = next(line for line in lines if line.startswith("User:"))
        off_topic = next(line for line in lines if line.startswith("Assistant:"))
        assert len(on_topic) > len(off_topic)
        assert len(off_topic) <= len("Assistant: ") + _SUMMARY_CHARS_PER_MESSAGE + 1

    def test_guidance_is_capped(self) -> None:
        result = compact_on_request(_conversation(3), _COUNTER, guidance="x" * 2000)
        assert result is not None
        summary = content_as_text(result[0][1].content)
        assert "x" * 500 in summary and "x" * 501 not in summary

    def test_nothing_to_summarize(self) -> None:
        assert compact_on_request(_conversation(1), _COUNTER) is None
        assert compact_on_request(_conversation(3)[1:], _COUNTER) is None  # no system
        assert compact_on_request(_conversation(3), _COUNTER, from_position=2) is None
        assert compact_on_request(_conversation(3), _COUNTER, from_position=99) is None


# ── Session.compact_now ──────────────────────────────────────────────────


class TestCompactNow:
    async def test_manual_emits_compacted_and_usage(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        assert await session.compact_now("the parser") is None
        (compacted,) = _events(session, ContextCompacted)
        assert compacted.trigger == "manual"
        assert compacted.guidance == "the parser"
        assert compacted.dropped_messages == 4
        (usage,) = _events(session, ContextUsage)
        assert usage.used_tokens == compacted.tokens_after
        assert usage.tier == "brain"
        assert usage.context_window == 32768
        assert usage.compact_at == budget_threshold(_tier())
        summary = content_as_text(session.conversation[1].content)
        assert summary.splitlines()[0] == f"{_SUMMARY_HEADER} (2 user turns)"

    async def test_from_here_names_its_trigger(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        assert await session.compact_now(user_index=1) is None
        (compacted,) = _events(session, ContextCompacted)
        assert compacted.trigger == "from_here"
        assert compacted.guidance is None
        assert _texts(session.conversation[:3]) == ["You are helpful.", "question 0", "answer 0"]

    async def test_user_index_counts_turns_already_compacted(self, tmp_path: Path) -> None:
        session = _session(tmp_path, turns=4)
        assert await session.compact_now() is None  # turns 0-2 summarized
        assert await session.compact_now(user_index=1) == "user_turn_compacted"
        # The window still calls the surviving turn index 3.
        session.conversation.extend([_msg("user", "question 4"), _msg("assistant", "answer 4")])
        assert await session.compact_now(user_index=4) is None
        users = [m for m in session.conversation if m.role == "user"]
        assert _texts(users) == ["question 3"]

    async def test_turns_before_a_from_here_summary_keep_their_numbers(
        self, tmp_path: Path
    ) -> None:
        session = _session(tmp_path, turns=5)
        assert await session.compact_now(user_index=2) is None  # turns 2-4 summarized
        session.conversation.extend([_msg("user", "question 5"), _msg("assistant", "answer 5")])
        # Turn 3 is inside the summary: refused, and nothing changes.
        before = _texts(session.conversation)
        assert await session.compact_now(user_index=3) == "user_turn_compacted"
        assert _texts(session.conversation) == before
        # Turn 1 is still word for word: it is the start of a new summary.
        assert await session.compact_now(user_index=1) is None
        users = [m for m in session.conversation if m.role == "user"]
        assert _texts(users) == ["question 0"]
        # Turns 1-5 are now one summary; turn 6 follows it.
        session.conversation.extend([_msg("user", "question 6"), _msg("assistant", "answer 6")])
        assert await session.compact_now(user_index=5) == "user_turn_compacted"
        assert await session.compact_now(user_index=7) == "user_turn_not_found"
        assert await session.compact_now(user_index=6) is None
        assert _texts([m for m in session.conversation if m.role == "user"]) == ["question 0"]

    async def test_numbering_survives_a_restart(self, tmp_path: Path) -> None:
        session = _session(tmp_path, turns=5)
        assert await session.compact_now() is None  # turns 0-3 summarized
        session.conversation.extend([_msg("user", "question 5"), _msg("assistant", "answer 5")])
        saved = [chat_message_to_dict(m) for m in session.conversation]

        # A restart revives the saved conversation into a fresh session.
        revived = Session(str(tmp_path))
        revived.conversation[:] = [chat_message_from_dict(d) for d in json.loads(json.dumps(saved))]
        revived.context_meter = _meter()
        assert await revived.compact_now(user_index=2) == "user_turn_compacted"
        assert await revived.compact_now(user_index=5) is None
        assert _texts([m for m in revived.conversation if m.role == "user"]) == ["question 4"]

    async def test_a_summary_written_before_the_turn_count_is_read_by_its_lines(
        self, tmp_path: Path
    ) -> None:
        session = Session(str(tmp_path))
        session.context_meter = _meter()
        legacy = f"{_SUMMARY_HEADER}\nUser: question 0\nAssistant: answer 0\nUser: question 1"
        session.conversation.extend(
            [
                _msg("system", "You are helpful."),
                _msg("system", legacy),
                *_conversation(4)[5:],  # turns 2 and 3
            ]
        )
        assert await session.compact_now(user_index=1) == "user_turn_compacted"
        assert await session.compact_now(user_index=3) is None
        assert _texts([m for m in session.conversation if m.role == "user"]) == ["question 2"]

    async def test_refusals(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        assert await session.compact_now(user_index=7) == "user_turn_not_found"
        session._open_turns = 1
        assert await session.compact_now() == "turn_in_progress"
        session._open_turns = 0

        fresh = Session(str(tmp_path))
        fresh.conversation.extend(_conversation(3))
        assert await fresh.compact_now() == "nothing_to_compact"  # no turn measured yet
        one_turn = _session(tmp_path, turns=1)
        assert await one_turn.compact_now() == "nothing_to_compact"
        assert not _events(session, ContextCompacted)


# ── the loop reports usage ───────────────────────────────────────────────


async def test_loop_reports_usage_before_each_model_call(tmp_path: Path) -> None:
    config = _forge_config(context_window=65536, max_output_tokens=4096)
    mock = MockProvider(default=Script(kind="stream", content="Hi there."))
    session = Session(str(tmp_path))
    runner = await _run(session, mock, config, tmp_path)
    try:
        await session.add_user_message("hello")
        await wait_for_turn(session, 1, 15.0)
    finally:
        await runner.cancel()

    usage = _events(session, ContextUsage)
    assert len(usage) == len(mock.calls) >= 1
    first = usage[0]
    assert first.tier == "brain"  # the role, as TierState names it
    assert first.context_window == 65536
    assert first.compact_at == budget_threshold(_tier(65536, 4096))
    assert 0 < first.used_tokens < first.compact_at
    assert session.context_meter is not None


# ── the daemon message ───────────────────────────────────────────────────


async def _send(daemon: Daemon, payload: dict) -> dict | None:
    """The daemon's direct reply (an error frame), or None."""
    raw = await daemon._handle_message(json.dumps(payload), None)
    if raw is None:
        return None
    reply = json.loads(raw)
    assert reply["type"] == "error", raw
    return reply


async def test_daemon_compacts_and_explains_refusals(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    session = await daemon.session_registry.create(str(tmp_path))

    refused = await _send(daemon, {"type": "compact_context", "session_id": session.id})
    assert refused is not None
    assert refused["code"] == "nothing_to_compact"
    assert refused["message"].startswith("Nothing to compact yet")
    assert refused["session_id"] == session.id

    session.conversation.extend(_conversation(3))
    session.context_meter = _meter()
    reply = await _send(
        daemon,
        {"type": "compact_context", "session_id": session.id, "guidance": "keep tests"},
    )
    assert reply is None
    (compacted,) = _events(session, ContextCompacted)
    assert compacted.guidance == "keep tests"

    missing = await _send(daemon, {"type": "compact_context", "session_id": "nope"})
    assert missing is not None and missing["code"] == "session_not_found"


# ── the window's fixtures match the daemon's models ──────────────────────


def test_ui_fixtures_parse_as_daemon_events() -> None:
    from tstd.protocol import ToolProgress, parse_daemon_event

    fixtures = json.loads(
        (Path(__file__).parents[2] / "ui/src/lib/protocol-fixtures.json").read_text()
    )
    usage = parse_daemon_event(json.dumps(fixtures["context_usage"]))
    assert isinstance(usage, ContextUsage) and usage.tier == "brain"
    manual = parse_daemon_event(json.dumps(fixtures["context_compacted_manual"]))
    assert isinstance(manual, ContextCompacted) and manual.trigger == "manual"
    plain = parse_daemon_event(json.dumps(fixtures["context_compacted"]))
    assert isinstance(plain, ContextCompacted) and plain.trigger == "auto"
    progress = parse_daemon_event(json.dumps(fixtures["tool_progress"]))
    assert isinstance(progress, ToolProgress)
