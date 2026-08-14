"""Tests for context window management (TD-405).

Covers: budget threshold math, token estimation, compaction-point
safety (tool-call pairs never split, in-flight turn always kept),
summary fold-in across successive compactions, timeline announcement,
and steering re-injection after compaction (assembly re-reads disk).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.test_loop import mock_factory, wait_for_turn
from tstd.compaction import (
    KEEP_USER_TURNS,
    THRESHOLD_FRACTION,
    budget_threshold,
    estimate_tokens,
    find_compaction_point,
    maybe_compact,
)
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context import AssembledPrompt, PromptAssembler
from tstd.context.tokens import HeuristicTokenCounter
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted
from tstd.provider import ChatMessage, FunctionCall
from tstd.provider import ToolCall as ProviderToolCall
from tstd.router import TierName, TierRouter
from tstd.session import Session, SessionRunner

COUNTER = HeuristicTokenCounter()  # deterministic: ceil(chars / 4)


def tiny_config(context_window: int = 600, max_output_tokens: int = 50) -> ModelConfig:
    """A config whose brain window is small enough to compact in tests."""
    tiers = {
        name: TierConfig(
            slug=f"test-{name}",
            base_url="http://mock.local/v1",
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        for name in ("brain", "worker", "validator")
    }
    preset = Preset(brain=tiers["brain"], worker=tiers["worker"], validator=tiers["validator"])
    return ModelConfig(
        presets={"test": preset},
        active_preset="test",
    )


def tiny_tier(context_window: int = 600, max_output_tokens: int = 50) -> TierConfig:
    return tiny_config(context_window, max_output_tokens).tier("brain")


def pad(prefix: str, chars: int) -> str:
    """A message body of exactly *chars* characters (4 chars ≈ 1 token)."""
    return (prefix + " " + "x" * chars)[:chars]


def turn(user: str, assistant: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=user),
        ChatMessage(role="assistant", content=assistant),
    ]


# ── Budget and estimation ──────────────────────────────────────────────


def test_budget_threshold_reserves_output_tokens() -> None:
    tier = tiny_tier(context_window=1000, max_output_tokens=100)
    assert budget_threshold(tier) == int(900 * THRESHOLD_FRACTION)


def tool_call(tc_id: str, name: str, arguments: str) -> ProviderToolCall:
    return ProviderToolCall(id=tc_id, function=FunctionCall(name=name, arguments=arguments))


def test_estimate_counts_content_and_tool_arguments() -> None:
    messages = [
        ChatMessage(role="system", content=pad("sys", 40)),
        ChatMessage(role="user", content=pad("u", 80)),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[tool_call("tc1", "shell", pad("cmd", 40))],
        ),
        ChatMessage(role="tool", content=pad("out", 60), tool_call_id="tc1"),
    ]
    estimated, method = estimate_tokens(messages, COUNTER)
    # ceil(chars/4): 10 + 20 + 10 + 15 = 55
    assert estimated == 55
    assert method == HeuristicTokenCounter().count("x").method


# ── Compaction point ───────────────────────────────────────────────────


def test_no_cut_when_at_or_below_kept_turns() -> None:
    messages = [ChatMessage(role="system", content="s")]
    for i in range(KEEP_USER_TURNS):
        messages += turn(f"u{i}", f"a{i}")
    assert find_compaction_point(messages) == 0


def test_cut_lands_on_a_user_boundary() -> None:
    messages = [ChatMessage(role="system", content="s")]
    for i in range(4):
        messages += turn(f"u{i}", f"a{i}")
    cut = find_compaction_point(messages)
    assert messages[cut].role == "user"
    assert messages[cut].content == "u2"  # exactly the last two turns kept


def test_maybe_compact_is_noop_below_budget() -> None:
    tier = tiny_tier()
    messages = [ChatMessage(role="system", content="s")]
    messages += turn("u1", "a1")
    result, stats = maybe_compact(messages, tier, COUNTER)
    assert stats is None
    assert result is messages


def test_maybe_compact_noop_when_nothing_old_to_drop() -> None:
    """Over budget but everything is a recent turn: proceed uncompacted
    rather than corrupt the live conversation."""
    tier = tiny_tier()
    messages = [ChatMessage(role="system", content=pad("sys", 100))]
    messages += turn(pad("u1", 800), pad("a1", 800))
    messages += turn(pad("u2", 800), pad("a2", 800))
    result, stats = maybe_compact(messages, tier, COUNTER)
    assert stats is None
    assert result == messages


def test_tool_call_pairs_never_split_by_compaction() -> None:
    tier = tiny_tier(context_window=500, max_output_tokens=25)
    assistant_calls = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[tool_call("tc1", "fs_read", pad("args", 200))],
    )
    tool_result = ChatMessage(role="tool", content=pad("result", 200), tool_call_id="tc1")
    messages = [
        ChatMessage(role="system", content=pad("sys", 100)),
        ChatMessage(role="user", content=pad("u1", 300)),
        ChatMessage(role="assistant", content=pad("a1", 200)),
        ChatMessage(role="user", content=pad("u2", 300)),
        # The call/result pair sits inside the span that will be kept.
        assistant_calls,
        tool_result,
        ChatMessage(role="user", content=pad("u3", 300)),
    ]
    result, stats = maybe_compact(messages, tier, COUNTER)
    assert stats is not None
    # Kept span starts on a user message...
    assert result[2].role == "user"
    # ...and the pair is kept intact and adjacent.
    pair_index = result.index(assistant_calls)
    assert result[pair_index + 1] is tool_result
    # Structural invariant: every kept tool call has its result nearby.
    kept_calls = [tc.id for m in result if m.tool_calls for tc in m.tool_calls]
    kept_results = {m.tool_call_id for m in result if m.role == "tool"}
    assert set(kept_calls) <= kept_results


# ── Summary behavior ───────────────────────────────────────────────────


def test_summary_replaces_dropped_turns() -> None:
    tier = tiny_tier(context_window=400, max_output_tokens=25)
    messages = [ChatMessage(role="system", content=pad("sys", 80))]
    for i in range(4):
        messages += turn(pad(f"user{i}", 240), pad(f"assist{i}", 240))
    result, stats = maybe_compact(messages, tier, COUNTER)
    assert stats is not None
    assert result[0].content == messages[0].content  # system untouched
    assert result[1].role == "system"
    assert "[Earlier conversation compacted]" in (result[1].content or "")
    assert "User: user0" in (result[1].content or "")
    assert "Assistant: assist0" in (result[1].content or "")
    # Last two user turns survive verbatim.
    assert [m.content for m in result if m.role == "user"] == [
        pad("user2", 240),
        pad("user3", 240),
    ]
    assert stats.tokens_after < stats.tokens_before
    assert stats.dropped_messages == 4  # u0, a0, u1, a1


def test_second_compaction_folds_prior_summary_in() -> None:
    """The oldest context must degrade gradually, not vanish: a summary
    dropped by a later compaction folds into the new summary."""
    tier = tiny_tier(context_window=500, max_output_tokens=25)
    messages = [ChatMessage(role="system", content=pad("sys", 80))]
    for i in range(3):
        messages += turn(pad(f"first{i}", 280), pad(f"assist-first{i}", 280))
    once, stats = maybe_compact(messages, tier, COUNTER)
    assert stats is not None

    for i in range(2):
        once += turn(pad(f"second{i}", 280), pad(f"assist-second{i}", 280))
    twice, stats = maybe_compact(once, tier, COUNTER)
    assert stats is not None
    summary = twice[1].content or ""
    # The prior summary folded in: its marker line survives inside.
    assert summary.count("[Earlier conversation compacted]") == 2
    # And the new drop is described too.
    assert "first2" in summary


# ── Loop level ─────────────────────────────────────────────────────────


def _seed_workspace(workspace: Path) -> None:
    """Sync helper: file I/O stays out of async functions (ASYNC240)."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("Test workspace steering.\n")


async def run_session(
    workspace: Path,
    home: Path,
    messages_to_send: list[str],
    config: ModelConfig,
    response_chars: int = 800,
) -> tuple[Session, MockProvider, SessionRunner, list[str]]:
    """Drive a scripted session; returns the session, mock, runner, and the
    cache-prefix hash of every assembled prompt, in order."""
    _seed_workspace(workspace)
    assembler = PromptAssembler(workspace, home_dir=home)
    prefix_hashes: list[str] = []
    orig_assemble = assembler.assemble

    async def _recorded_assemble(tier: TierName, **kwargs: Any) -> AssembledPrompt:
        result = await orig_assemble(tier, **kwargs)
        prefix_hashes.append(result.prefix_hash)
        return result

    assembler.assemble = _recorded_assemble  # type: ignore[method-assign]
    session = Session(str(workspace))
    mock = MockProvider(
        scripts={"test-brain": Script(kind="stream", content=pad("reply", response_chars))}
    )
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            mock_factory(mock),
            config,
            prompt_assembler=assembler,
        ),
    )
    await runner.start()
    for i, content in enumerate(messages_to_send, start=1):
        await session.add_user_message(content)
        await wait_for_turn(session, i)
    return session, mock, runner, prefix_hashes


async def test_compaction_fires_and_is_announced(tmp_path: Path) -> None:
    session, mock, runner, _hashes = await run_session(
        tmp_path / "ws",
        tmp_path / "home",
        [pad("turn1", 800), pad("turn2", 800), pad("turn3", 800)],
        tiny_config(),
    )

    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    # Turn 2 is over budget but has nothing old to drop; turn 3 compacts.
    assert len(events) == 1
    event = events[0]
    assert event.dropped_messages > 0
    assert event.tokens_before > event.tokens_after

    # The request that followed compaction carries the fresh system
    # message at index 0 and the summary at index 1.
    sent = mock.calls[-1].messages
    assert sent[0].role == "system"
    assert "Test workspace steering" in (sent[0].content or "")
    assert "[Earlier conversation compacted]" in (sent[1].content or "")
    assert sent[2].role == "user"
    await runner.cancel()


async def test_system_prompt_fresh_from_disk_after_compaction(tmp_path: Path) -> None:
    """Steering edited mid-session is re-read from disk on the very next
    send — compaction must not stale it (AC: instructions survive)."""
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    session, mock, runner, _hashes = await run_session(
        ws, home, [pad("turn1", 800), pad("turn2", 800)], tiny_config()
    )
    (ws / "AGENTS.md").write_text("REPLACED-STEERING: always answer tersely.\n")
    await session.add_user_message(pad("turn3", 800))
    await wait_for_turn(session, 3)

    sent = mock.calls[-1].messages
    assert sent[0].role == "system"
    assert "REPLACED-STEERING" in (sent[0].content or "")
    # Compaction happened in this session (over-budget third turn).
    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    assert len(events) >= 1
    await runner.cancel()


async def test_prefix_stable_across_compactions(tmp_path: Path) -> None:
    """Compaction never perturbs the cache prefix: it only rewrites the
    conversation tail.  (Full system *text* varies by tier — worker tier
    injects the task — so the invariant is on the prefix hash, matching
    the loop's own cache key.)"""
    _session, _mock, runner, prefix_hashes = await run_session(
        tmp_path / "ws",
        tmp_path / "home",
        [pad(f"turn{i}", 800) for i in range(1, 4)],
        tiny_config(),
    )
    assert len(prefix_hashes) >= 3
    assert len(set(prefix_hashes)) == 1
    await runner.cancel()
