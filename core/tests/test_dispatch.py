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
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.protocol import TurnComplete
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import Tool, ToolDispatcher, ToolRegistry, UnclassifiedToolCall
from tstd.tools.boundary import PathGuard

# ── Helpers ─────────────────────────────────────────────────────────────


async def _stub_worker(prompt: str) -> str:
    """Stub worker-tier classifier: always answers B (fail toward asking)."""
    return "B"


def attach_auto_approver(dispatcher: ToolDispatcher) -> ToolDispatcher:
    """TD-802: the policy gate is always on.  Dispatch-mechanics tests
    exercise dispatch, not policy — auto-approve anything resolving to ask
    (the stub classifier answers B for ambiguous calls).
    """
    dispatcher.policy = PolicyConfig()

    async def _auto_approve(*_args: object) -> ApprovalOutcome:
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _auto_approve
    return dispatcher


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

    dispatcher = attach_auto_approver(
        ToolDispatcher(registry, classifier=make_classifier(), max_result_chars=1000)
    )

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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier())
        )
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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier())
        )
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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier())
        )

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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier(), max_result_chars=100)
        )

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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier())
        )

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
        dispatcher = attach_auto_approver(  # TD-802
            ToolDispatcher(registry, classifier=make_classifier())
        )

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


# ── Mixed-batch ordering and failure isolation (TD-607) ────────────────


def make_mixed_dispatcher() -> ToolDispatcher:
    """A dispatcher with one parallel-safe and one sequential tool.

    Every pre-TD-607 ``dispatch_many`` test used a batch of a single tool
    type, so the partition boundary was never crossed within one call —
    which is why the reordering survived.
    """
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="par_tool",
            description="Parallel-safe",
            parameters={
                "type": "object",
                "properties": {"delay": {"type": "number", "default": 0.0}},
                "required": [],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )
    registry.register(
        Tool(
            name="seq_tool",
            description="Not parallel-safe",
            parameters={
                "type": "object",
                "properties": {"delay": {"type": "number", "default": 0.0}},
                "required": [],
            },
            side_effect_class="ask",
            parallel_safe=False,
        )
    )
    dispatcher = attach_auto_approver(ToolDispatcher(registry, classifier=make_classifier()))

    async def handler(session, delay=0.0, tool_call_id=""):
        import asyncio

        if delay:
            await asyncio.sleep(delay)
        return f"ran {tool_call_id}"

    dispatcher.register_handler("par_tool", handler)
    dispatcher.register_handler("seq_tool", handler)
    return dispatcher


class TestDispatchManyOrdering:
    async def test_mixed_batch_returns_results_in_input_order(self) -> None:
        """A batch mixing both partitions comes back in the caller's order.

        Order is keyed on input position, not on tool_call_id: the id is
        client-supplied and carries no uniqueness guarantee.
        """
        dispatcher = make_mixed_dispatcher()

        results = await dispatcher.dispatch_many(
            [("c1", "seq_tool", {}), ("c2", "par_tool", {}), ("c3", "seq_tool", {})]
        )

        assert [r.tool_call_id for r in results] == ["c1", "c2", "c3"]
        assert [r.name for r in results] == ["seq_tool", "par_tool", "seq_tool"]
        assert all(r.status == "success" for r in results)

    async def test_order_holds_when_tool_call_ids_repeat(self) -> None:
        """Duplicate ids in one batch still map back to their own slot."""
        dispatcher = make_mixed_dispatcher()

        results = await dispatcher.dispatch_many(
            [("dup", "par_tool", {}), ("dup", "seq_tool", {}), ("dup", "par_tool", {})]
        )

        assert [r.name for r in results] == ["par_tool", "seq_tool", "par_tool"]

    async def test_mixed_batch_still_parallelises_the_safe_tools(self) -> None:
        """Restoring input order must not serialise the parallel batch."""
        import asyncio

        dispatcher = make_mixed_dispatcher()

        start = asyncio.get_running_loop().time()
        results = await dispatcher.dispatch_many(
            [
                ("c1", "par_tool", {"delay": 0.05}),
                ("c2", "seq_tool", {"delay": 0.05}),
                ("c3", "par_tool", {"delay": 0.05}),
                ("c4", "par_tool", {"delay": 0.05}),
            ]
        )
        elapsed = asyncio.get_running_loop().time() - start

        # 3 parallel (~0.05s together) + 1 sequential (0.05s) ≈ 0.10s.
        # Fully serialised would be ~0.20s.
        assert elapsed < 0.16
        assert [r.tool_call_id for r in results] == ["c1", "c2", "c3", "c4"]

    async def test_raising_handler_in_parallel_batch_does_not_escape(self) -> None:
        """A handler that raises becomes an error result for its own call
        and leaves its siblings alone."""
        dispatcher = make_mixed_dispatcher()

        async def boom(session, delay=0.0, tool_call_id=""):
            raise RuntimeError("handler exploded")

        dispatcher.register_handler("par_tool", boom)

        results = await dispatcher.dispatch_many(
            [("c1", "seq_tool", {}), ("c2", "par_tool", {}), ("c3", "seq_tool", {})]
        )

        assert [r.tool_call_id for r in results] == ["c1", "c2", "c3"]
        assert results[1].status == "error"
        assert "handler exploded" in results[1].output
        assert results[0].status == "success"
        assert results[2].status == "success"

    async def test_dispatch_failure_in_parallel_batch_becomes_concurrent_error(self) -> None:
        """An exception from *outside* the handler's own try/except is
        reported as a ``concurrent_error`` result for that call.

        A handler returning ``None`` instead of a string is the realistic
        shape: the handler returns cleanly, then truncation blows up past
        the point where dispatch guards itself.  ``gather`` was collecting
        that exception and ``task.result()`` was re-raising it, so it left
        ``dispatch_many`` and took the healthy siblings with it.
        """
        dispatcher = make_mixed_dispatcher()

        async def returns_none(session, delay=0.0, tool_call_id=""):
            return None

        dispatcher.register_handler("par_tool", returns_none)

        results = await dispatcher.dispatch_many(
            [("c1", "seq_tool", {}), ("c2", "par_tool", {}), ("c3", "seq_tool", {})]
        )

        assert [r.tool_call_id for r in results] == ["c1", "c2", "c3"]
        failed = results[1]
        assert failed.status == "error"
        assert failed.error_code == "concurrent_error"
        assert failed.name == "par_tool"  # not "" — the loop keys events on this
        assert failed.output.startswith("Concurrent dispatch failed:")
        assert results[0].status == "success"
        assert results[2].status == "success"

    async def test_chokepoint_bypass_still_raises_from_a_parallel_batch(self) -> None:
        """Failure isolation does not soften the classifier chokepoint
        (prime §2.6): a call reaching execution unguarded still raises out
        of ``dispatch_many``, as it does on the sequential path."""
        dispatcher = make_mixed_dispatcher()
        dispatcher.approval_handler = None  # the ask gate is part of the chokepoint

        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch_many([("c1", "par_tool", {}), ("c2", "seq_tool", {})])


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


# ── Touch-tracking (TD-503) ────────────────────────────────────────────


def _make_touch_dispatcher(ws: Path) -> ToolDispatcher:
    """A dispatcher with a fake path-bearing read tool over a real workspace."""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="fake_read",
            description="Reads a path",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            side_effect_class="auto",
            parallel_safe=True,
            path_fields=("path",),
        )
    )
    return attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=make_classifier(str(ws)),
            path_guard=PathGuard(Boundary(workspace_root=ws)),
            workspace=ws,
        )
    )


class TestTouchTracking:
    """Dispatch records a successful call's path targets on the session so
    path-scoped rules can activate at the next assembly.  Raw relative
    paths resolve against the process CWD (the guard has no notion of
    relative-to-workspace), so these chdir into the workspace — the verdict
    then pins identically on every platform."""

    async def test_success_records_workspace_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.chdir(ws)
        session = Session(str(ws))
        dispatcher = _make_touch_dispatcher(ws)

        async def _read(**_kwargs: object) -> str:
            return "file contents"

        dispatcher.register_handler("fake_read", _read)
        result = await dispatcher.dispatch(
            "c1", "fake_read", {"path": "src/app.py"}, session=session
        )
        assert result.status == "success"
        assert session.touched_paths == {"src/app.py"}

    async def test_boundary_refusal_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        (tmp_path / "secret.txt").write_text("shh\n", encoding="utf-8")
        monkeypatch.chdir(ws)
        session = Session(str(ws))
        dispatcher = _make_touch_dispatcher(ws)

        async def _must_not_run(**_kwargs: object) -> str:
            raise AssertionError("handler ran despite the refusal")

        dispatcher.register_handler("fake_read", _must_not_run)
        result = await dispatcher.dispatch(
            "c2", "fake_read", {"path": "../secret.txt"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "boundary_refusal"
        assert session.touched_paths == set()

    async def test_handler_error_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        monkeypatch.chdir(ws)
        session = Session(str(ws))
        dispatcher = _make_touch_dispatcher(ws)

        async def _fails(**_kwargs: object) -> str:
            raise RuntimeError("disk on fire")

        dispatcher.register_handler("fake_read", _fails)
        result = await dispatcher.dispatch(
            "c3", "fake_read", {"path": "notes.txt"}, session=session
        )
        assert result.status == "error"
        assert result.error_code == "handler_error"
        assert session.touched_paths == set()
