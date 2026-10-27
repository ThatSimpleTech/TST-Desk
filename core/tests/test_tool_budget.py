"""TD-5173: a tier defers builtin tools behind tool_search; TD-5174: reasoning echo."""

from __future__ import annotations

from dataclasses import replace

from tstd.compaction import estimate_tokens
from tstd.config import McpToolSearchConfig, TierConfig
from tstd.context.tokens import HeuristicTokenCounter
from tstd.mcp.deferral import McpToolDeferral
from tstd.provider import ChatCompletionRequest, ChatMessage, FunctionCall, ToolCall, ToolDefinition
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.tool_search import TOOL_SEARCH_NAME, tool_search

COUNTER = HeuristicTokenCounter()


def _tier(deferred: list[str] | None = None, echo: bool = False) -> TierConfig:
    return TierConfig(
        slug="m",
        base_url="http://127.0.0.1:9/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=65536,
        max_output_tokens=100,
        deferred_tools=deferred or [],
        echo_reasoning=echo,
    )


def _names(defs: list[ToolDefinition] | None) -> list[str]:
    return [d.function.name for d in defs or () if d.function is not None]


def _offer(session: Session, tier: TierConfig, messages: list[ChatMessage] | None = None):
    registry = create_registry()
    deferral = McpToolDeferral(registry, McpToolSearchConfig())
    defs = deferral.offer(
        registry.to_provider_definitions(),
        session=session,
        tier="brain",
        tier_cfg=tier,
        counter=COUNTER,
        messages=messages or [],
    )
    return registry, defs


def test_no_patterns_keeps_every_builtin_and_hides_tool_search() -> None:
    session = Session("/tmp/ws")
    _, defs = _offer(session, _tier())
    names = _names(defs)
    assert "github_merge" in names and "browser_click" in names
    assert TOOL_SEARCH_NAME not in names
    assert session.deferred_builtin_tools == []


def test_patterns_hold_builtins_back_and_name_them_in_tool_search() -> None:
    session = Session("/tmp/ws")
    _, defs = _offer(session, _tier(["github_*", "desktop_*", "browser_*"]))
    names = _names(defs)
    assert not any(n.startswith(("github_", "desktop_", "browser_")) for n in names)
    assert "fs_read" in names and "shell" in names and TOOL_SEARCH_NAME in names
    search = next(d for d in defs or () if d.function and d.function.name == TOOL_SEARCH_NAME)
    assert "Other tools you can load:" in search.function.description
    assert "browser_click" in search.function.description
    assert "browser_click" in session.deferred_builtin_tools


async def test_tool_search_loads_a_deferred_builtin_for_later_steps() -> None:
    session = Session("/tmp/ws")
    tier = _tier(["browser_*"])
    registry, _ = _offer(session, tier)
    out = await tool_search(registry=registry, session=session, query="select:browser_navigate")
    assert "Loaded 1 tool(s)" in out
    _, defs = _offer(session, tier)
    assert "browser_navigate" in _names(defs) and "browser_click" not in _names(defs)


def test_a_called_deferred_builtin_stays_offered() -> None:
    session = Session("/tmp/ws")
    called = ChatMessage(
        role="assistant",
        tool_calls=[
            ToolCall(
                id="1",
                type="function",
                function=FunctionCall(name="github_get_pull", arguments="{}"),
            )
        ],
    )
    _, defs = _offer(session, _tier(["github_*"]), [called])
    names = _names(defs)
    assert "github_get_pull" in names and "github_merge" not in names


def test_reasoning_content_rides_on_the_wire_only_when_set() -> None:
    from tstd.provider import ChatCompletionRequest

    _message_to_dict = ChatCompletionRequest._message_to_dict

    with_reasoning = ChatMessage(
        role="assistant", content=None, reasoning_content="plan: read files"
    )
    assert _message_to_dict(with_reasoning)["reasoning_content"] == "plan: read files"
    assert "reasoning_content" not in _message_to_dict(ChatMessage(role="assistant", content="hi"))


def test_tier_settings_default_off() -> None:
    tier = TierConfig(
        slug="m",
        base_url="http://127.0.0.1:9/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=100,
    )
    assert tier.deferred_tools == [] and tier.echo_reasoning is False


# ── echoed thinking stays in the running turn (TD-5177) ─────────────────


def _step(thinking: str) -> ChatMessage:
    call = ToolCall(id="c1", function=FunctionCall(name="fs_read", arguments="{}"))
    return ChatMessage(role="assistant", content="", tool_calls=[call], reasoning_content=thinking)


def _two_turns() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="first"),
        _step("old thinking " * 50),
        ChatMessage(role="tool", content="ok", tool_call_id="c1"),
        ChatMessage(role="assistant", content="done"),
        ChatMessage(role="user", content="second"),
        _step("new thinking"),
        ChatMessage(role="tool", content="ok", tool_call_id="c1"),
        ChatMessage(role="user", content="keep going", steer=True),
        _step("more thinking"),
    ]


def test_only_the_running_turn_sends_its_thinking() -> None:
    body = ChatCompletionRequest(model="m", messages=_two_turns()).to_dict()
    sent = [m.get("reasoning_content") for m in body["messages"]]
    assert "old thinking " * 50 not in sent
    # A steer joins the turn, so thinking before it still goes out.
    assert "new thinking" in sent
    assert "more thinking" in sent


def test_estimate_counts_the_thinking_that_is_sent() -> None:
    counter = HeuristicTokenCounter()
    messages = _two_turns()
    with_thinking = estimate_tokens(messages, counter)[0]
    bare = [replace(m, reasoning_content=None) for m in messages]
    without = estimate_tokens(bare, counter)[0]
    assert with_thinking > without
    # The earlier turn's thinking is not sent, so it is not counted.
    assert with_thinking - without < counter.count("old thinking " * 50).count
