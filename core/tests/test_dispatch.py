"""Tests for tool dispatch (TD-402).

Covers: ToolDispatcher validation, execution, truncation, parallel
dispatch, and loop integration (tool calls executed, results fed back
to the model, multi-round-trip tool call loops).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.protocol import TurnComplete
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import Tool, ToolDispatcher, ToolRegistry

# ── Helpers ─────────────────────────────────────────────────────────────


async def _stub_worker(prompt: str) -> str:
    """Stub worker-tier classifier: always answers B (fail toward asking)."""
    return "B"


def make_classifier(workspace: str = "/tmp/ws") -> AmbiguousClassifier:
    """A classifier over a stub workspace for dispatch tests.

    TD-702/703 make classification a mandatory chokepoint: a tool reaching
    execution without a classifier raises ``UnclassifiedToolCall``.  These
    dispatcher-mechanics tests attach one with a stub worker so dispatch
    itself is exercised; ambiguous calls classify as B.
    """
    return AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=Path(workspace))),
        call_worker=_stub_worker,
    )


def make_config() -> ModelConfig:
    """A minimal ModelConfig with distinct model slugs per tier."""

    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url="http://mock.local/v1",
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=100_000,
            max_output_tokens=1_000,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("test-brain"),
                worker=tier("test-worker"),
                validator=tier("test-validator"),
            ),
        },
        active_preset="test",
    )


def make_registry_and_dispatcher() -> tuple[ToolRegistry, ToolDispatcher]:
    """Create a registry with a test tool and a dispatcher with a handler."""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Echo arguments back",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "count": {"type": "integer", "default": 1},
                },
                "required": ["message"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )
    registry.register(
        Tool(
            name="slow_tool",
            description="A tool that takes time",
            parameters={
                "type": "object",
                "properties": {"delay": {"type": "number", "default": 0.01}},
                "required": [],
            },
            side_effect_class="ask",
            parallel_safe=False,
        )
    )

    dispatcher = ToolDispatcher(registry, classifier=make_classifier(), max_result_chars=1000)

    async def echo_handler(session, message, count=1, tool_call_id=""):
        return f"Echo: {message} (x{count})"

    async def slow_handler(session, delay=0.01, tool_call_id=""):
        import asyncio

        await asyncio.sleep(delay)
        return "Done"

    dispatcher.register_handler("echo", echo_handler)
    dispatcher.register_handler("slow_tool", slow_handler)

    return registry, dispatcher


def mock_factory(mock: MockProvider):
    """Return a factory that always returns the given mock."""

    async def _factory():
        return mock

    return _factory


async def wait_for_turn(session: Session, n: int, _timeout: float = 3.0) -> TurnComplete:
    """Wait until the n-th TurnComplete event exists and return it."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + _timeout
    while asyncio.get_running_loop().time() < deadline:
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        if len(completes) >= n:
            return completes[n - 1]
        await asyncio.sleep(0.02)
    raise TimeoutError(f"turn {n} did not complete within {_timeout}s")


async def start_loop(
    session: Session,
    router: TierRouter,
    mock: MockProvider,
    config: ModelConfig,
    registry: ToolRegistry | None = None,
    dispatcher: ToolDispatcher | None = None,
) -> SessionRunner:
    """Start a SessionRunner running the real agent loop."""
    factory = mock_factory(mock)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            router,
            factory,
            config,
            tool_registry=registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    return runner


# ── ToolDispatcher unit tests ───────────────────────────────────────────


class TestDispatcherValidation:
    def test_register_handler_requires_registered_tool(self) -> None:
        registry = ToolRegistry()
        dispatcher = ToolDispatcher(registry, classifier=make_classifier())
        with pytest.raises(KeyError):
            dispatcher.register_handler("unknown_tool", lambda: "x")

    async def test_unknown_tool_name_returns_error(self) -> None:
        _registry, dispatcher = make_registry_and_dispatcher()
        result = await dispatcher.dispatch("call_1", "bogus_tool", {})
        assert result.status == "error"
        assert result.error_code == "unknown_tool"
        assert "bogus_tool" in result.output

    async def test_missing_required_field_returns_error(self) -> None:
        _registry, dispatcher = make_registry_and_dispatcher()
        # echo requires "message", but we pass empty args
        result = await dispatcher.dispatch("call_1", "echo", {})
        assert result.status == "error"
        assert result.error_code == "invalid_arguments"
        assert "message" in result.output.lower() or "required" in result.output.lower()

    async def test_missing_handler_returns_error(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="orphan_tool",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(registry, classifier=make_classifier())
        # Tool is registered but has no handler
        result = await dispatcher.dispatch("call_1", "orphan_tool", {})
        assert result.status == "error"
        assert result.error_code == "no_handler"


class TestDispatcherExecution:
    async def test_successful_dispatch(self) -> None:
        _registry, dispatcher = make_registry_and_dispatcher()
        result = await dispatcher.dispatch("call_1", "echo", {"message": "hello"})
        assert result.status == "success"
        assert result.output == "Echo: hello (x1)"
        assert result.tool_call_id == "call_1"
        assert result.truncated is False

    async def test_default_values_applied(self) -> None:
        _registry, dispatcher = make_registry_and_dispatcher()
        result = await dispatcher.dispatch("call_1", "echo", {"message": "hi", "count": 3})
        assert result.status == "success"
        assert result.output == "Echo: hi (x3)"

    async def test_handler_error_is_caught(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="bad_tool",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(registry, classifier=make_classifier())

        async def failing_handler(session, **kwargs):
            raise RuntimeError("Something went wrong")

        dispatcher.register_handler("bad_tool", failing_handler)
        result = await dispatcher.dispatch("call_1", "bad_tool", {})
        assert result.status == "error"
        assert result.error_code == "handler_error"
        assert "Something went wrong" in result.output


class TestDispatcherTruncation:
    async def test_truncation_with_marker(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="big_tool",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(registry, classifier=make_classifier(), max_result_chars=100)

        async def big_handler(session, **kwargs):
            return "X" * 500

        dispatcher.register_handler("big_tool", big_handler)
        result = await dispatcher.dispatch("call_1", "big_tool", {})
        assert result.truncated is True
        assert len(result.output) < 200  # truncated
        assert "truncated" in result.output

    async def test_no_truncation_for_small_output(self) -> None:
        _registry, dispatcher = make_registry_and_dispatcher()
        result = await dispatcher.dispatch("call_1", "echo", {"message": "tiny"})
        assert result.truncated is False
        assert result.output == "Echo: tiny (x1)"


class TestDispatcherParallel:
    async def test_parallel_safe_tools_run_concurrently(self) -> None:
        """Parallel-safe tools should complete faster than sequential."""
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="fast_tool",
                parameters={
                    "type": "object",
                    "properties": {"delay": {"type": "number", "default": 0.05}},
                    "required": [],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(registry, classifier=make_classifier())

        async def fast_handler(session, delay=0.05, tool_call_id=""):
            import asyncio

            await asyncio.sleep(delay)
            return "Fast result"

        dispatcher.register_handler("fast_tool", fast_handler)

        import asyncio

        start = asyncio.get_running_loop().time()
        results = await dispatcher.dispatch_many(
            [
                ("c1", "fast_tool", {"delay": 0.05}),
                ("c2", "fast_tool", {"delay": 0.05}),
                ("c3", "fast_tool", {"delay": 0.05}),
            ]
        )
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 0.15  # parallel: 3 x 0.05s should finish in ~0.05s
        assert len(results) == 3
        assert all(r.status == "success" for r in results)

    async def test_non_parallel_tools_run_sequentially(self) -> None:
        """Non-parallel-safe tools should NOT run concurrently."""
        import asyncio

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="seq_tool",
                parameters={
                    "type": "object",
                    "properties": {"delay": {"type": "number", "default": 0.05}},
                    "required": [],
                },
                side_effect_class="ask",
                parallel_safe=False,
            )
        )
        dispatcher = ToolDispatcher(registry, classifier=make_classifier())

        async def seq_handler(session, delay=0.05, tool_call_id=""):
            await asyncio.sleep(delay)
            return "Sequential result"

        dispatcher.register_handler("seq_tool", seq_handler)

        start = asyncio.get_running_loop().time()
        results = await dispatcher.dispatch_many(
            [("c1", "seq_tool", {"delay": 0.05}), ("c2", "seq_tool", {"delay": 0.05})]
        )
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed >= 0.09  # sequential: 2 x 0.05s should take ~0.1s
        assert len(results) == 2


# ── Loop integration ────────────────────────────────────────────────────


class TestLoopIntegration:
    async def test_tool_call_dispatched_and_result_streamed(self) -> None:
        """A tool call turn dispatches the tool and emits ToolResult event."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hello world"}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Say hello")
        await wait_for_turn(session, 1)

        # Should have a ToolResult event
        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 1
        tr = tool_results[0]
        assert tr.status == "success"
        assert tr.output == "Echo: hello world (x1)"
        assert tr.tool_call_id == "call_mock_1"

        await runner.cancel()

    async def test_tool_call_error_returns_structured_error_to_model(self) -> None:
        """Invalid arguments produce a structured error, not a turn failure."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        # The model calls echo without the required "message" arg
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"wrong_key": "nope"}',
                    ),
                    Script(kind="stream", content="Retrying"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Call echo badly")
        _tc = await wait_for_turn(session, 1)

        # The turn completes (doesn't fail) — the error goes to the model
        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 1
        tr = tool_results[0]
        assert tr.status == "error"
        # The error message should be descriptive enough for the model to fix itself
        assert "invalid" in tr.output.lower() or "message" in tr.output.lower()

        await runner.cancel()

    async def test_tool_call_round_trip_feeds_result_back_to_model(self) -> None:
        """After tool dispatch, the result is in the conversation history."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        # First call: tool call, second call: text response
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "roundtrip"}',
                    ),
                    Script(kind="stream", content="The echo returned roundtrip"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Test roundtrip")

        # Wait for the first turn to complete
        _tc = await wait_for_turn(session, 1)

        # The mock should have been called at least twice:
        # 1. First call: tool call response
        # 2. Second call: text response (with tool result in history)
        assert len(mock.calls) >= 2
        second_call = mock.calls[1]
        tool_messages = [m for m in second_call.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        assert "roundtrip" in (tool_messages[0].content or "")

        await runner.cancel()

    async def test_multi_tool_call_round_trips(self) -> None:
        """Tool call round-trips continue until the model returns text."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        # Two tool-call round-trips, then a text response.
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "first"}',
                    ),
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "second"}',
                    ),
                    Script(kind="stream", content="Both done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Do multiple calls")

        # Wait for first turn - the tool call round-trips happen within it
        _tc = await wait_for_turn(session, 1)

        # Three provider calls: tool_call, tool_call, text
        assert len(mock.calls) == 3

        # Two ToolResults: one per round-trip
        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 2
        assert "first" in tool_results[0].output
        assert "second" in tool_results[1].output

        # Both tool results are in the conversation for the final call
        third_call = mock.calls[2]
        tool_messages = [m for m in third_call.messages if m.role == "tool"]
        assert len(tool_messages) == 2

        await runner.cancel()

    async def test_dispatch_event_ordering(self) -> None:
        """tool_call emitted before tool_result, both before turn_complete, in seq order."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "ordered"}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Test ordering")
        await wait_for_turn(session, 1)
        await runner.cancel()

        # Collect events in seq order
        events = sorted(session.event_log.all_events, key=lambda e: e.seq)
        seqs_of: dict[str, list[int]] = {}
        for e in events:
            seqs_of.setdefault(e.type, []).append(e.seq)

        # Both tool_call and tool_result fire exactly once
        assert len(seqs_of["tool_call"]) == 1
        assert len(seqs_of["tool_result"]) == 1
        # tool_call strictly precedes tool_result
        assert seqs_of["tool_call"][0] < seqs_of["tool_result"][0]
        # tool_result precedes turn_complete
        assert seqs_of["tool_result"][0] < seqs_of["turn_complete"][0]
        # seq is contiguous
        all_seqs = [e.seq for e in events]
        assert all_seqs == list(range(1, len(all_seqs) + 1))

    async def test_tool_definitions_sent_to_provider(self) -> None:
        """When a registry is provided, tool definitions are sent to the model."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        _registry, dispatcher = make_registry_and_dispatcher()

        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hello")})

        runner = await start_loop(session, router, mock, config, _registry, dispatcher)
        await session.add_user_message("Hi")
        await wait_for_turn(session, 1)

        # The provider request should include tools
        assert len(mock.calls) >= 1
        request = mock.calls[0]
        # Check the request dict for tools
        body = request.to_dict()
        assert "tools" in body
        tool_names = [t["function"]["name"] for t in body["tools"]]
        assert "echo" in tool_names

        await runner.cancel()


# ── Dispatcher without registry ─────────────────────────────────────────


class TestWithoutDispatcher:
    async def test_loop_works_without_dispatcher(self) -> None:
        """Without a dispatcher, tool calls are emitted but not executed."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="tool_call",
                    tool_name="echo",
                    tool_arguments='{"message": "hello"}',
                )
            }
        )

        # No registry or dispatcher — TD-401 mode
        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Do something")
        await wait_for_turn(session, 1)

        # Tool calls are emitted as events but no ToolResult
        tool_events = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
        assert len(tool_events) == 1
        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 0

        await runner.cancel()
