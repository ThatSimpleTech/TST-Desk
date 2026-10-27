"""A long running turn trims its own older steps (TD-5178).

One request that runs for hours is a single turn, so compaction keeps
all of it. Its echoed thinking and the text of every file it wrote then
filled the window while the trimmer only looked at tool results.
"""

from __future__ import annotations

import json

from tstd.compaction import budget_threshold, estimate_tokens
from tstd.config import TierConfig
from tstd.context.tokens import HeuristicTokenCounter
from tstd.context_fit import KEEP_RECENT_STEPS, fit_inflight_turn, shorten_call_arguments
from tstd.provider import ChatMessage, FunctionCall, ToolCall

_COUNTER = HeuristicTokenCounter()


def _tier() -> TierConfig:
    return TierConfig(
        slug="local-model",
        base_url="http://mock.local/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=65536,
        max_output_tokens=16384,
    )


def _long_turn(steps: int = 30) -> list[ChatMessage]:
    messages = [
        ChatMessage(role="system", content="You build apps."),
        ChatMessage(role="user", content="Build the whole app."),
    ]
    for n in range(steps):
        body = f"// file {n}\n" + "const x = 1;\n" * 500
        args = json.dumps({"path": f"src/f{n}.ts", "content": body})
        messages.append(
            ChatMessage(
                role="assistant",
                content="",
                reasoning_content=f"step {n} thinking " * 300,
                tool_calls=[
                    ToolCall(id=f"c{n}", function=FunctionCall(name="fs_write", arguments=args))
                ],
            )
        )
        messages.append(
            ChatMessage(role="tool", tool_call_id=f"c{n}", content=f"wrote src/f{n}.ts")
        )
    return messages


def test_older_steps_lose_thinking_and_file_text_first() -> None:
    tier = _tier()
    messages = _long_turn()
    assert estimate_tokens(messages, _COUNTER)[0] > budget_threshold(tier)

    fitted, fit = fit_inflight_turn(messages, tier, _COUNTER)

    assert fit is not None and fit.trimmed_steps > 0
    assert estimate_tokens(fitted, _COUNTER)[0] <= budget_threshold(tier)
    steps = [m for m in fitted if m.role == "assistant"]
    for step in steps[-KEEP_RECENT_STEPS:]:
        assert step.reasoning_content
        assert len(step.tool_calls[0].function.arguments) > 5000
    assert steps[0].reasoning_content is None
    trimmed = json.loads(steps[0].tool_calls[0].function.arguments)
    assert trimmed["path"] == "src/f0.ts"
    assert "chars elided from this call" in trimmed["content"]
    # Every result keeps its call, and the results are untouched.
    assert [m.tool_call_id for m in fitted if m.role == "tool"] == [f"c{n}" for n in range(30)]
    assert all("elided" not in str(m.content) for m in fitted if m.role == "tool")


def test_a_turn_that_fits_is_left_alone() -> None:
    messages = _long_turn(steps=3)
    fitted, fit = fit_inflight_turn(messages, _tier(), _COUNTER)
    assert fit is None and fitted is messages


def test_earlier_turns_are_not_trimmed_here() -> None:
    earlier = _long_turn()[1:]
    messages = [
        ChatMessage(role="system", content="s"),
        *earlier,
        ChatMessage(role="user", content="Next request."),
    ]
    _fitted, fit = fit_inflight_turn(messages, _tier(), _COUNTER)
    # Compaction owns earlier turns; the running one has no steps yet.
    assert fit is None


def test_arguments_that_are_not_json_stay_whole() -> None:
    raw = "not json " * 200
    assert shorten_call_arguments(raw) == raw
    short = json.dumps({"path": "a.ts"})
    assert shorten_call_arguments(short) == short


def test_a_long_earlier_turn_is_folded_even_though_two_turns_are_kept() -> None:
    from tstd.compaction import auto_span, maybe_compact

    tier = _tier()
    messages = [
        *_long_turn(),
        ChatMessage(role="user", content="Now check it builds."),
    ]
    span = auto_span(messages, tier, _COUNTER)
    assert span == (1, len(messages) - 1)
    compacted, stats = maybe_compact(messages, tier, _COUNTER)
    assert stats is not None and stats.dropped_user_turns == 1
    assert compacted[-1].content == "Now check it builds."
    assert estimate_tokens(compacted, _COUNTER)[0] <= budget_threshold(tier)


def test_two_short_turns_still_keep_both() -> None:
    from tstd.compaction import find_compaction_point, maybe_compact

    tier = _tier()
    filler = [
        ChatMessage(role="user", content="old " * 40000),
        ChatMessage(role="assistant", content="ok"),
    ]
    messages = [
        ChatMessage(role="system", content="s"),
        *filler,
        ChatMessage(role="user", content="one"),
        ChatMessage(role="assistant", content="done"),
        ChatMessage(role="user", content="two"),
    ]
    compacted, stats = maybe_compact(messages, tier, _COUNTER)
    assert stats is not None
    assert compacted[2:] == messages[find_compaction_point(messages) :]
