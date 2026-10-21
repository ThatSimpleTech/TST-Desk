"""Clear stale tool results before whole turns are compacted (TD-5115).

Over the compaction budget, earlier turns' tool results become a
one-line placeholder (tool, target, size), all in one batch, which has
to land at 70% of the budget or under. The newest results and excluded
tools stay, a clear too small to be worth a prefix rebuild does not
happen, and the prompt is byte-stable between clears. Loop tests use
MockProvider.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path

import pytest

from tests.test_context_fit import _forge_config, _tier
from tests.test_loop import mock_factory, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.compaction import budget_threshold, estimate_tokens
from tstd.config import ClearToolResultsConfig, ModelConfig, load_config
from tstd.context.tokens import HeuristicTokenCounter
from tstd.context_clear import (
    CLEARED_PREFIX,
    clear_stale_tool_results,
    cleared_placeholder,
    is_cleared,
    stale_tool_indexes,
)
from tstd.context_fit import ELIDE_TARGET_FRACTION
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import (
    ContextCompacted,
    Question,
    QuestionAnswer,
    QuestionOption,
    parse_daemon_event,
)
from tstd.provider import ChatMessage, FunctionCall, content_as_text
from tstd.provider import ToolCall as ProviderToolCall
from tstd.questions import QuestionReply, format_reply
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

_FIXTURES = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "protocol-fixtures.json"
COUNTER = HeuristicTokenCounter()  # ceil(chars / 4)
TIER = _tier()  # 32k window, 4k output: budget 22,937 tokens
BUDGET = budget_threshold(TIER)
TARGET = int(BUDGET * ELIDE_TARGET_FRACTION)
ON = ClearToolResultsConfig()


def _call(call_id: str, tool: str, arguments: dict[str, object]) -> ProviderToolCall:
    return ProviderToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=tool, arguments=json.dumps(arguments)),
    )


def _turn(
    n: int, chars: int = 12_000, tool: str = "fs_read", call_id: str | None = None
) -> list[ChatMessage]:
    """One finished turn: a read of ``docs/file<n>.md`` and an answer."""
    call_id = f"t{n}" if call_id is None else call_id
    body = (f"file{n} line " * (chars // 10 + 1))[:chars]
    return [
        ChatMessage(role="user", content=f"turn {n}: what does file {n} say?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[_call(call_id, tool, {"path": f"docs/file{n}.md"})],
        ),
        ChatMessage(role="tool", content=body, tool_call_id=call_id),
        ChatMessage(role="assistant", content=f"File {n} says hello."),
    ]


def _conversation(
    turns: int, chars: int = 12_000, call_id: str | None = None, **tools: str
) -> list[ChatMessage]:
    """System, *turns* finished turns, then the user message now in flight.

    *call_id* gives every turn's call the same id, as MockProvider does
    (``call_mock_1``) and as a provider that sends none does (``""``).
    """
    messages = [ChatMessage(role="system", content="system")]
    for n in range(1, turns + 1):
        messages.extend(_turn(n, chars, tools.get(f"t{n}", "fs_read"), call_id))
    messages.append(ChatMessage(role="user", content="and now?"))
    return messages


def _tool_texts(messages: Sequence[ChatMessage]) -> list[str]:
    return [content_as_text(m.content) for m in messages if m.role == "tool"]


def _wire(messages: Sequence[ChatMessage]) -> list[str]:
    return [json.dumps(asdict(m), sort_keys=True, default=str) for m in messages]


# ── The clear ──────────────────────────────────────────────────────────


def test_under_the_budget_nothing_changes() -> None:
    messages = _conversation(4)
    assert estimate_tokens(messages, COUNTER)[0] <= BUDGET
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert out is messages
    assert stats is None


def test_old_results_become_a_placeholder_naming_tool_target_and_size() -> None:
    messages = _conversation(8)
    before = estimate_tokens(messages, COUNTER)[0]
    assert before > BUDGET

    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)

    assert stats is not None
    texts = _tool_texts(out)
    cleared = [t for t in texts if t.startswith(CLEARED_PREFIX)]
    assert texts[0] == (
        '[Earlier tool result cleared to save context: fs_read "docs/file1.md", '
        "12,000 chars. Run the tool again if you need it.]"
    )
    # One batch takes every stale result: all but the newest three.
    assert texts[:5] == cleared
    assert texts[5:] == _tool_texts(messages)[5:]
    assert stats.cleared_results == len(cleared) == 5
    # Messages stay: roles, ids and the calls are where they were.
    assert len(out) == len(messages) == stats.kept_messages
    assert [m.role for m in out] == [m.role for m in messages]
    assert [m.tool_call_id for m in out] == [m.tool_call_id for m in messages]
    assert [m.tool_calls for m in out] == [m.tool_calls for m in messages]
    assert [m.content for m in out if m.role != "tool"] == [
        m.content for m in messages if m.role != "tool"
    ]
    # Triggered at the budget, it lands under the trim's 70% target.
    assert stats.tokens_before == before
    assert stats.tokens_after == estimate_tokens(out, COUNTER)[0] <= TARGET


def test_the_newest_results_are_kept_whatever_turn_they_are_in() -> None:
    messages = _conversation(12)
    out, _ = clear_stale_tool_results(
        messages, TIER, COUNTER, ClearToolResultsConfig(clear_at_least_tokens=0)
    )
    texts = _tool_texts(out)
    assert texts[-3:] == _tool_texts(messages)[-3:]
    assert all(text.startswith(CLEARED_PREFIX) for text in texts[:-3])

    keep_five = ClearToolResultsConfig(keep_recent_tool_results=5, clear_at_least_tokens=0)
    out, _ = clear_stale_tool_results(messages, TIER, COUNTER, keep_five)
    texts = _tool_texts(out)
    assert texts[-5:] == _tool_texts(messages)[-5:]
    assert all(text.startswith(CLEARED_PREFIX) for text in texts[:-5])


def test_this_turns_results_are_left_to_the_in_turn_trim() -> None:
    messages = _conversation(0)
    calls = [_call(f"c{i}", "fs_read", {"path": f"big{i}.md"}) for i in range(8)]
    messages.append(ChatMessage(role="assistant", content=None, tool_calls=calls))
    for i in range(8):
        messages.append(ChatMessage(role="tool", content="y" * 12_000, tool_call_id=f"c{i}"))
    assert estimate_tokens(messages, COUNTER)[0] > BUDGET

    policy = ClearToolResultsConfig(keep_recent_tool_results=0, clear_at_least_tokens=0)
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert out is messages
    assert stats is None


def test_a_message_sent_mid_turn_does_not_make_this_turns_results_stale() -> None:
    """A steer (TD-5112) joins the running turn; it starts none, so the
    results the turn got before it are still this turn's."""
    messages = _conversation(0)
    calls = [_call(f"c{i}", "fs_read", {"path": f"big{i}.md"}) for i in range(8)]
    messages.append(ChatMessage(role="assistant", content=None, tool_calls=calls))
    for i in range(8):
        messages.append(ChatMessage(role="tool", content="y" * 12_000, tool_call_id=f"c{i}"))
    messages.append(ChatMessage(role="user", content="also check big9.md", steer=True))
    assert estimate_tokens(messages, COUNTER)[0] > BUDGET

    policy = ClearToolResultsConfig(keep_recent_tool_results=0, clear_at_least_tokens=0)
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert out is messages
    assert stats is None

    # An earlier turn's result before the running turn is still fair game.
    earlier = _conversation(1)[:-1] + messages[1:]
    out, stats = clear_stale_tool_results(earlier, TIER, COUNTER, policy)
    assert stats is not None and stats.cleared_results == 1
    assert _tool_texts(out)[0].startswith(CLEARED_PREFIX)
    assert _tool_texts(out)[1:] == _tool_texts(earlier)[1:]


def test_an_excluded_tool_keeps_its_result() -> None:
    messages = _conversation(8, t1="memory_lookup")
    policy = ClearToolResultsConfig(exclude_tools=["memory_lookup"])
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert stats is not None
    texts = _tool_texts(out)
    assert texts[0] == _tool_texts(messages)[0]
    assert texts[1].startswith(CLEARED_PREFIX)


def _asked_turn() -> tuple[list[ChatMessage], str]:
    """Turn 1 asks the person (``ask_user``) and gets a worded answer."""
    question = Question(
        question="Which database should the app use, given the deploy target?",
        options=[QuestionOption(label="Postgres"), QuestionOption(label="SQLite")],
    )
    words = "Postgres, but keep the schema portable: we may move to the managed one. " * 4
    answer = format_reply(
        [question], QuestionReply("answered", (QuestionAnswer(selected=["Postgres"], text=words),))
    )
    messages = _conversation(9)
    messages[2] = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[_call("t1", "ask_user", {"questions": [question.model_dump()]})],
    )
    messages[3] = ChatMessage(role="tool", content=answer, tool_call_id="t1")
    return messages, answer


def test_an_ask_user_answer_is_never_cleared_whatever_the_config() -> None:
    # TD-5121: an ask_user result is the person's own answer, their words.
    # Its placeholder would tell the model to run the tool again: to ask
    # the person the same thing again, or to guess.
    messages, answer = _asked_turn()
    placeholder = cleared_placeholder("ask_user", "", len(answer))
    assert COUNTER.count(answer).count > COUNTER.count(placeholder).count  # worth clearing
    for policy in (ON, ClearToolResultsConfig(exclude_tools=["memory_lookup"])):
        assert 3 not in stale_tool_indexes(messages, policy)
        out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
        assert stats is not None
        assert content_as_text(out[3].content) == answer
        assert _tool_texts(out)[1].startswith(CLEARED_PREFIX)


def _placeholder_files(messages: Sequence[ChatMessage]) -> list[str]:
    """The target each cleared result names, oldest first."""
    return [text.split('"')[1] for text in _tool_texts(messages) if text.startswith(CLEARED_PREFIX)]


def test_a_result_is_named_after_its_own_call_when_ids_repeat_across_turns() -> None:
    # MockProvider gives every turn's call ``call_mock_1``, and the loop
    # keeps whatever id the provider sends: a result answers the call of
    # its own assistant message, not the last call that used the id.
    for call_id in ("call_mock_1", ""):
        messages = _conversation(8, call_id=call_id)
        out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
        assert stats is not None
        assert _placeholder_files(out) == [f"docs/file{n}.md" for n in range(1, 6)], call_id


def test_an_excluded_tool_keeps_its_result_when_ids_repeat_across_turns() -> None:
    messages = _conversation(8, call_id="call_mock_1", t1="memory_lookup")
    policy = ClearToolResultsConfig(exclude_tools=["memory_lookup"])
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert stats is not None
    texts = _tool_texts(out)
    assert texts[0] == _tool_texts(messages)[0]
    assert texts[1] == cleared_placeholder("fs_read", "docs/file2.md", 12_000)


def _parallel_turn(call_ids: tuple[str, str], tools: tuple[str, str]) -> list[ChatMessage]:
    """A finished turn of two parallel reads, results in call order."""
    calls = [
        _call(call_id, tool, {"path": f"docs/{tool}.md"})
        for call_id, tool in zip(call_ids, tools, strict=True)
    ]
    body = "x" * 12_000
    return [
        ChatMessage(role="user", content="read both"),
        ChatMessage(role="assistant", content=None, tool_calls=calls),
        *(ChatMessage(role="tool", content=body, tool_call_id=c) for c in call_ids),
        ChatMessage(role="assistant", content="Both read."),
    ]


def test_parallel_calls_sharing_an_id_pair_with_their_results_in_order() -> None:
    for call_ids in (("same", "same"), ("", "")):
        messages = _conversation(6)
        messages[1:1] = _parallel_turn(call_ids, ("fs_read", "memory_lookup"))
        policy = ClearToolResultsConfig(exclude_tools=["memory_lookup"])
        out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
        assert stats is not None
        texts = _tool_texts(out)
        assert texts[0] == cleared_placeholder("fs_read", "docs/fs_read.md", 12_000)
        assert texts[1] == _tool_texts(messages)[1], call_ids


def test_a_result_with_no_call_of_its_own_is_left_alone() -> None:
    # Its id names no call of the assistant message before it, so neither
    # the tool nor exclude_tools can be told: it stays as it is.
    messages = _conversation(8)
    messages[3] = replace(messages[3], tool_call_id="t2")
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert stats is not None
    texts = _tool_texts(out)
    assert texts[0] == _tool_texts(messages)[0]
    assert _placeholder_files(out) == [f"docs/file{n}.md" for n in range(2, 6)]


def test_a_clear_that_cannot_free_enough_does_not_happen() -> None:
    messages = _conversation(8)
    stale_tokens = sum(COUNTER.count(t).count for t in _tool_texts(messages)[:-3])
    policy = ClearToolResultsConfig(clear_at_least_tokens=stale_tokens + 1)
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert out is messages
    assert stats is None


def test_a_clear_that_frees_just_enough_happens() -> None:
    messages = _conversation(8)
    stale = _tool_texts(messages)[:-3]
    placeholders = (
        len(stale) * COUNTER.count(cleared_placeholder("fs_read", "docs/file1.md", 12_000)).count
    )
    freed = sum(COUNTER.count(t).count for t in stale) - placeholders
    policy = ClearToolResultsConfig(clear_at_least_tokens=freed)
    _, stats = clear_stale_tool_results(messages, TIER, COUNTER, policy)
    assert stats is not None
    assert stats.tokens_before - stats.tokens_after == freed


def _long_answers(messages: list[ChatMessage], chars: int) -> None:
    """Give every finished turn's answer *chars* characters."""
    for i, message in enumerate(messages):
        if message.role == "assistant" and not message.tool_calls:
            messages[i] = ChatMessage(role="assistant", content="a" * chars)


def test_a_clear_that_would_land_above_the_target_leaves_it_to_compaction() -> None:
    # Long answers cannot be cleared: clearing every stale result would
    # still leave the prompt over 70% of the budget, so another clear
    # would follow within a turn or two. Compaction makes the cut.
    messages = _conversation(8, chars=4_000)
    _long_answers(messages, 8_000)
    assert estimate_tokens(messages, COUNTER)[0] > BUDGET
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert out is messages
    assert stats is None


def test_with_no_older_turn_to_fold_a_high_landing_still_clears() -> None:
    # Two user turns: compaction keeps both, so it cannot help, and the
    # clear goes ahead even though it lands above 70% of the budget.
    messages = _conversation(1, chars=40_000)
    _long_answers(messages, 60_000)
    calls = [_call(f"c{i}", "fs_read", {"path": f"now{i}.md"}) for i in range(3)]
    messages.append(ChatMessage(role="assistant", content=None, tool_calls=calls))
    for i in range(3):
        messages.append(ChatMessage(role="tool", content="z" * 4_000, tool_call_id=f"c{i}"))
    assert estimate_tokens(messages, COUNTER)[0] > BUDGET
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert stats is not None
    assert stats.tokens_after > TARGET
    assert is_cleared(out[3])


def test_switched_off_it_never_clears() -> None:
    messages = _conversation(12)
    out, stats = clear_stale_tool_results(
        messages, TIER, COUNTER, ClearToolResultsConfig(enabled=False)
    )
    assert out is messages
    assert stats is None


def test_short_results_are_not_replaced_by_a_longer_line() -> None:
    messages = _conversation(9)
    messages[3] = ChatMessage(role="tool", content="ok", tool_call_id="t1")
    out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert stats is not None
    assert content_as_text(out[3].content) == "ok"


def test_an_image_result_says_what_went() -> None:
    messages = _conversation(8)
    messages[3] = ChatMessage(
        role="tool",
        content=[
            {"type": "text", "text": "x" * 12_000},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        tool_call_id="t1",
    )
    out, _ = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert out[3].content == cleared_placeholder("fs_read", "docs/file1.md", 12_000, images=1)
    assert "12,000 chars and 1 image." in content_as_text(out[3].content)


def test_the_target_is_one_line_and_short() -> None:
    line = cleared_placeholder("shell", "", 10)
    assert line.startswith(CLEARED_PREFIX)
    messages = _conversation(8)
    command = "grep -rn\n  'needle' " + "src/" * 40
    messages[2] = ChatMessage(
        role="assistant", content=None, tool_calls=[_call("t1", "shell", {"command": command})]
    )
    out, _ = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    text = content_as_text(out[3].content)
    assert "\n" not in text
    target = text.split('"')[1]
    assert target.startswith("grep -rn 'needle' src/")
    assert len(target) == 80 and target.endswith("…")


# ── Batches and a byte-stable prefix ───────────────────────────────────


def _grow(messages: list[ChatMessage], n: int) -> None:
    """Turn *n* - 1, in flight, finishes, and turn *n* starts."""
    messages.extend(_turn(n - 1)[1:])
    messages.append(_turn(n)[0])


def test_clears_come_in_batches_and_the_prefix_holds_between_them() -> None:
    messages = [ChatMessage(role="system", content="system"), _turn(1)[0]]
    sent: list[list[str]] = []
    cleared_at: list[int] = []
    for n in range(2, 40):
        _grow(messages, n)
        out, stats = clear_stale_tool_results(messages, TIER, COUNTER, ON)
        if stats is not None:
            cleared_at.append(len(sent))
            messages[:] = out
        assert estimate_tokens(messages, COUNTER)[0] <= BUDGET, "no compaction is needed"
        sent.append(_wire(messages))

    assert len(cleared_at) >= 5
    # A batch buys room for several turns: never two clears in a row.
    gaps = [b - a for a, b in pairwise(cleared_at)]
    assert min(gaps) >= 2
    for call in range(1, len(sent)):
        previous, current = sent[call - 1], sent[call]
        if call in cleared_at:
            continue
        # Between clears the last request is a byte-exact prefix.
        assert current[: len(previous)] == previous, f"prefix changed at call {call}"
    # A placeholder, once written, is never rewritten.
    final = {i: line for i, line in enumerate(sent[-1]) if CLEARED_PREFIX in line}
    for request in sent:
        for i, line in enumerate(request):
            if i in final and CLEARED_PREFIX in line:
                assert line == final[i]


def test_a_cleared_result_is_not_cleared_again() -> None:
    messages = _conversation(8)
    out, _ = clear_stale_tool_results(messages, TIER, COUNTER, ON)
    assert is_cleared(out[3])
    again = list(out)
    again[-1:] = [
        *(m for n in range(9, 14) for m in _turn(n)),
        ChatMessage(role="user", content="next"),
    ]
    later, stats = clear_stale_tool_results(again, TIER, COUNTER, ON)
    assert stats is not None
    assert later[3] is out[3]


# ── Config ─────────────────────────────────────────────────────────────


def test_the_shipped_config_turns_it_on_with_the_documented_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "config.yaml")
    policy = config.clear_tool_results
    assert policy == ClearToolResultsConfig()
    assert policy.enabled is True
    assert policy.keep_recent_tool_results == 3
    assert policy.clear_at_least_tokens == 2_000
    assert policy.exclude_tools == []


def test_exclude_tools_are_trimmed_and_deduplicated() -> None:
    policy = ClearToolResultsConfig.model_validate(
        {"exclude_tools": [" fs_read ", "fs_read", "", "shell"]}
    )
    assert policy.exclude_tools == ["fs_read", "shell"]


def test_the_clear_fixture_round_trips() -> None:
    raw = json.loads(_FIXTURES.read_text())["context_compacted_clear_stale"]
    event = parse_daemon_event(json.dumps(raw))
    assert isinstance(event, ContextCompacted)
    assert event.trigger == "clear_stale"
    # Every field an older window reads is still there.
    assert {"dropped_messages", "kept_messages", "tokens_before", "tokens_after"} <= set(raw)


# ── In the loop ────────────────────────────────────────────────────────


def _write_files(workspace: Path, count: int) -> None:
    for n in range(1, count + 1):
        line = f"file{n}-" + "x" * 70
        (workspace / f"file{n}.md").write_text("\n".join([line] * 100) + "\n", encoding="utf-8")


def _scripts(count: int) -> list[Script]:
    scripts: list[Script] = []
    for n in range(1, count + 1):
        scripts.append(
            Script(
                kind="tool_call",
                tool_name="fs_read",
                tool_arguments=json.dumps({"path": f"file{n}.md"}),
            )
        )
        scripts.append(Script(kind="stream", content=f"File {n} is about part {n}. " * 12))
    return scripts


async def _long_session(
    workspace: Path, config: ModelConfig, turns: int
) -> tuple[list[ContextCompacted], list[list[str]]]:
    """Drive *turns* read-then-answer turns; return the context events and
    every request's messages as the wire would carry them."""
    mock = MockProvider(sequences={"gateway-model": _scripts(turns)})
    session = Session(str(workspace))
    dispatcher = make_dispatcher(workspace)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(lead_turns=1_000),
            mock_factory(mock),
            config,
            tool_registry=dispatcher.registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    try:
        for n in range(1, turns + 1):
            await session.add_user_message(f"Turn {n}: read file{n}.md and tell me about it.")
            turn = await wait_for_turn(session, n, 15.0)
            assert turn.failed is False
    finally:
        await runner.cancel()
    assert len(mock.calls) == 2 * turns
    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    return events, [_wire(call.messages) for call in mock.calls]


def _break_points(requests: list[list[str]]) -> list[int]:
    """Requests that do not start with the previous request, byte for byte."""
    return [
        index
        for index, (previous, current) in enumerate(pairwise(requests), start=1)
        if current[: len(previous)] != previous
    ]


def _prefix_breaks(requests: list[list[str]]) -> int:
    return len(_break_points(requests))


def _misnamed_placeholders(requests: list[list[str]]) -> tuple[int, list[str]]:
    """Count the cleared results in *requests*; list those that do not name
    the file their own turn read (turn n reads ``file<n>.md``)."""
    cleared = 0
    wrong: list[str] = []
    for request in requests:
        turn = ""
        for line in request:
            message = json.loads(line)
            content = message["content"]
            if message["role"] == "user" and isinstance(content, str):
                if content.startswith("Turn "):
                    turn = content.split(":")[0].removeprefix("Turn ")
            elif message["role"] == "tool" and str(content).startswith(CLEARED_PREFIX):
                cleared += 1
                if f'fs_read "file{turn}.md"' not in content:
                    wrong.append(f"turn {turn}: {content}")
    return cleared, wrong


async def test_a_long_session_compacts_less_and_its_prompt_changes_only_in_batches(
    tmp_path: Path,
) -> None:
    turns = 30
    _write_files(tmp_path, turns)
    off = _forge_config().model_copy(
        update={"clear_tool_results": ClearToolResultsConfig(enabled=False)}
    )
    before, before_requests = await _long_session(tmp_path, off, turns)
    after, after_requests = await _long_session(tmp_path, _forge_config(), turns)

    compactions_before = [e for e in before if e.trigger == "auto"]
    compactions_after = [e for e in after if e.trigger == "auto"]
    clears = [e for e in after if e.trigger == "clear_stale"]
    assert len(compactions_before) >= 2
    assert len(compactions_after) < len(compactions_before)
    assert clears, "clearing never ran"
    for event in clears:
        assert event.dropped_messages >= 1
        assert event.tokens_before - event.tokens_after >= 2_000
    # The prompt changes only where an event says it did.
    assert _prefix_breaks(after_requests) == len(after)
    assert _prefix_breaks(before_requests) == len(before)
    # Clears are batches: each buys several turns before the next.
    assert len(clears) <= turns // 3
    points = _break_points(after_requests)
    assert min(b - a for a, b in pairwise(points)) >= 2
    # MockProvider sends every call as ``call_mock_1``; each placeholder
    # still names the file its own turn read.
    last = [json.loads(line) for line in after_requests[-1]]
    ids = [m["tool_call_id"] for m in last if m["role"] == "tool"]
    assert len(ids) > len(set(ids)), "the replay should repeat call ids"
    cleared, wrong = _misnamed_placeholders(after_requests)
    assert cleared > 0
    assert wrong == []


@pytest.mark.parametrize(
    ("tool", "target"),
    [
        ("shell", "rm -rf build && make"),
        ("delegate", "Fix the tests"),
        ("fs_write", "a.py"),
        ("github__create_issue", ""),
        ("", ""),
    ],
)
def test_a_tool_that_may_have_done_something_is_not_invited_to_run_again(
    tool: str, target: str
) -> None:
    """TD-5160: with approvals off, "run it again" would re-run a side effect."""
    line = cleared_placeholder(tool, target, 1_234)
    assert "Run the tool again" not in line
    assert line.endswith("It already ran; do not run it again only to see this output.]")
    for read in ("fs_read", "fs_grep", "web_fetch"):
        assert cleared_placeholder(read, "x", 1).endswith("Run the tool again if you need it.]")


def test_every_rerun_safe_tool_is_a_registered_read_only_tool() -> None:
    """A new read-only tool belongs here (TD-5129's session_search did)."""
    from tstd.context_clear import RERUN_SAFE_TOOLS
    from tstd.tools.registry import create_registry

    registered = {tool.name for tool in create_registry().list_tools()}
    assert RERUN_SAFE_TOOLS <= registered
    assert "session_search" in RERUN_SAFE_TOOLS
