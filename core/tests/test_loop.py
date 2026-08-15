"""Tests for the agent loop (TD-401).

Covers: loop inside SessionRunner, tier routing through the router,
provider-agnostic behaviour (MockProvider), tool call handling, and
multi-turn conversation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tests.test_dispatch import attach_auto_approver, make_classifier
from tstd.autonomy import Boundary
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, RuleActivated, TurnComplete
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import (
    Tool,
    ToolDispatcher,
    ToolRegistry,
    create_registry,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard

# ── Helpers ─────────────────────────────────────────────────────────────


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


def mock_factory(mock: MockProvider) -> Callable[[], Awaitable[MockProvider]]:
    """Return a factory that always returns the given mock."""

    async def _factory() -> MockProvider:
        return mock

    return _factory


async def wait_for_turn(session: Session, n: int, _timeout: float = 3.0) -> TurnComplete:
    """Wait until the n-th TurnComplete event exists and return it."""
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
) -> SessionRunner:
    """Start a SessionRunner running the real agent loop."""
    factory = mock_factory(mock)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(s, router, factory, config),
    )
    await runner.start()
    return runner


# ── Multi-turn conversation ─────────────────────────────────────────────


class TestMultiTurn:
    async def test_text_conversation_routes_tiers(self) -> None:
        """Brain handles first 2 turns, worker takes over on turn 3."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Brain reply"),
                "test-worker": Script(kind="stream", content="Worker reply"),
            }
        )

        runner = await start_loop(session, router, mock, config)

        await session.add_user_message("First")
        tc1 = await wait_for_turn(session, 1)
        assert tc1.tier == "brain"

        await session.add_user_message("Second")
        tc2 = await wait_for_turn(session, 2)
        assert tc2.tier == "brain"

        await session.add_user_message("Third")
        tc3 = await wait_for_turn(session, 3)
        assert tc3.tier == "worker"

        # The mock should have been called with the right models
        assert mock.calls[0].model == "test-brain"
        assert mock.calls[1].model == "test-brain"
        assert mock.calls[2].model == "test-worker"

        await runner.cancel()

    async def test_conversation_history_grows(self) -> None:
        """Each turn appends user + assistant messages to the request."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Reply one"),
                "test-worker": Script(kind="stream", content="Reply two"),
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hello")
        await wait_for_turn(session, 1)

        await session.add_user_message("Again")
        await wait_for_turn(session, 2)

        # Second request carries both turns of history
        second_request = mock.calls[1]
        roles = [m.role for m in second_request.messages]
        contents = [m.content for m in second_request.messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert contents[1] == "Hello"
        assert contents[2] == "Reply one"
        assert contents[3] == "Again"

        await runner.cancel()

    async def test_assistant_deltas_streamed(self) -> None:
        """Text deltas are emitted as AssistantDelta events."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={"test-brain": Script(kind="stream", content="Hello brave world")}
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hi")
        tc = await wait_for_turn(session, 1)

        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        text = "".join(d.delta for d in deltas)
        assert text == "Hello brave world"
        assert tc.tokens > 0  # usage recorded

        await runner.cancel()

    async def test_turn_complete_carries_cost_and_duration(self) -> None:
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="X")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hi")
        tc = await wait_for_turn(session, 1)

        assert tc.cost > 0  # priced from mock usage
        assert tc.duration >= 0
        assert tc.tier == "brain"

        await runner.cancel()


# ── Turn lifecycle event ordering (TD-403) ─────────────────────────────


class TestTurnEventOrdering:
    def _event_types(self, events) -> list[tuple[int, str]]:
        """Return [(seq, type)] for the given events, in seq order."""
        return [(e.seq, e.type) for e in sorted(events, key=lambda e: e.seq)]

    async def test_text_turn_emits_deltas_then_turn_complete(self) -> None:
        """A text turn: assistant_delta chunks then a single turn_complete."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="one two three")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Count to three")
        await wait_for_turn(session, 1)
        await runner.cancel()

        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        assert len(deltas) == 3  # "one", " two", " three"
        assert len(completes) == 1

        # Every delta appears before the turn_complete
        last_delta_seq = max(e.seq for e in deltas)
        complete_seq = completes[0].seq
        assert last_delta_seq < complete_seq

    async def test_tool_call_turn_ordering(self) -> None:
        """tool_call before tool_result (when dispatched), then turn_complete."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="echo",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        # TD-802: mechanics tests auto-approve so the policy gate's ask
        # (the loop-wired stub classifier answers B) does not park.
        dispatcher = attach_auto_approver(ToolDispatcher(registry))

        async def echo_handler(session, message, tool_call_id=""):
            return f"Echo: {message}"

        dispatcher.register_handler("echo", echo_handler)

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hi"}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        factory = mock_factory(mock)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, router, factory, config, tool_registry=registry, tool_dispatcher=dispatcher
            ),
        )
        await runner.start()
        await session.add_user_message("Say hi")
        await wait_for_turn(session, 1)
        await runner.cancel()

        ordered = self._event_types(session.event_log.all_events)

        # Exact per-type ordering across the whole turn:
        # any assistant_delta (content) < any tool_call < any tool_result < turn_complete
        seq_of_type: dict[str, list[int]] = {}
        for seq, typ in ordered:
            seq_of_type.setdefault(typ, []).append(seq)
        assert seq_of_type["tool_call"]
        assert seq_of_type["tool_result"]
        assert seq_of_type["turn_complete"]
        assert max(seq_of_type["tool_call"]) < max(seq_of_type["tool_result"])
        assert max(seq_of_type["tool_result"]) < max(seq_of_type["turn_complete"])

    async def test_no_dispatcher_emits_tool_call_but_no_tool_result(self) -> None:
        """Tool_call emitted, no tool_result, turn_complete still arrives."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="tool_call",
                    tool_name="echo",
                    tool_arguments='{"message": "hi"}',
                )
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Say hi")
        tc = await wait_for_turn(session, 1)
        await runner.cancel()

        tool_calls = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_calls) == 1
        assert len(tool_results) == 0
        assert isinstance(tc, TurnComplete)

    async def test_seq_is_monotonic_and_contiguous(self) -> None:
        """Every event carries a strictly increasing, gap-free seq."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="A B C D")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hi")
        await wait_for_turn(session, 1)
        await runner.cancel()

        seqs = sorted(e.seq for e in session.event_log.all_events)
        assert seqs == list(range(1, len(seqs) + 1))


# ── Tool calls ──────────────────────────────────────────────────────────


class TestToolCalls:
    async def test_tool_call_turn_emits_events(self) -> None:
        """A turn with tool calls emits ToolCall events and completes."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments='{"path": "/tmp/x.txt"}',
                )
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Read the file")
        await wait_for_turn(session, 1)

        tool_events = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
        assert len(tool_events) == 1
        ev = tool_events[0]
        assert ev.name == "fs_read"
        assert ev.arguments == {"path": "/tmp/x.txt"}
        assert ev.tool_call_id == "call_mock_1"

        await runner.cancel()

    async def test_tool_call_followed_by_text_turn(self) -> None:
        """Multi-turn: tool call turn, then a text turn, both succeed."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)  # keep everything on brain
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments='{"path": "/a"}',
                )
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Read /a")
        await wait_for_turn(session, 1)

        # Now script the second turn to reply with text
        mock.script(
            "test-brain",
            Script(kind="stream", content="Here is the content"),
        )
        await session.add_user_message("Summarize it")
        tc2 = await wait_for_turn(session, 2)

        # Conversation history includes the assistant tool_call message
        second_request = mock.calls[1]
        roles = [m.role for m in second_request.messages]
        assert "assistant" in roles
        assert tc2.tier == "brain"

        await runner.cancel()


# ── Error handling ──────────────────────────────────────────────────────


class TestErrors:
    async def test_provider_error_records_failure(self) -> None:
        """A provider error fails the turn and continues to the next."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="error",
                    error_code="server_error",
                    status_code=500,
                    content="boom",
                )
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Do something")
        await wait_for_turn(session, 1)

        # Failure is recorded in the router
        assert router.consecutive_failures == 1

        # The session is still alive — a second message works
        mock.script("test-brain", Script(kind="stream", content="Recovered"))
        await session.add_user_message("Try again")
        tc2 = await wait_for_turn(session, 2)
        assert tc2.tier == "brain"

        # Success resets the failure counter
        assert router.consecutive_failures == 0

        await runner.cancel()

    async def test_rate_limit_records_failure(self) -> None:
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="rate_limit", content="slow down")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Go")
        await wait_for_turn(session, 1)
        assert router.consecutive_failures == 1

        await runner.cancel()


# ── Runner integration ──────────────────────────────────────────────────


class TestRunnerIntegration:
    async def test_loop_runs_inside_session_runner(self) -> None:
        """The real loop runs in SessionRunner and completes state-wise."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hello")})

        runner = await start_loop(session, router, mock, config)
        assert session.state == "running"

        await session.add_user_message("Hello")
        await wait_for_turn(session, 1)

        await runner.cancel()
        assert session.state == "cancelled"

    async def test_cancellation_during_wait(self) -> None:
        """Cancelling while the loop waits for input ends it cleanly."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="X")})

        runner = await start_loop(session, router, mock, config)
        await asyncio.sleep(0.05)
        assert runner.is_running is True

        await runner.cancel()
        assert session.state == "cancelled"
        assert runner.is_running is False


# ── Cancellation (TD-404) ──────────────────────────────────────────────


class TestCancellation:
    async def test_cancellation_during_streaming(self) -> None:
        """Cancelling mid-stream stops the turn and preserves partial events."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        # Stream with chunk_delay so we can cancel mid-stream
        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="stream",
                    content="one two three four five",
                    chunk_delay=0.05,
                )
            }
        )

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hi")

        # Wait for streaming to start (first delta appears)
        for _ in range(50):
            deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
            if len(deltas) >= 1:
                break
            await asyncio.sleep(0.02)

        # Cancel mid-stream
        await runner.cancel()
        assert session.state == "cancelled"

        # The event log is append-only — partial deltas are preserved
        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        text = "".join(d.delta for d in deltas)
        assert text.startswith("one")  # at least the first word was streamed
        # No turn_complete was emitted for the cancelled turn
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        assert len(completes) == 0

    async def test_cancellation_during_tool_dispatch(self) -> None:
        """Cancelling before tool dispatch skips execution."""
        session = Session("/tmp/ws")
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="slow_tool",
                parameters={
                    "type": "object",
                    "properties": {"delay": {"type": "number", "default": 0.5}},
                    "required": [],
                },
                side_effect_class="auto",
                parallel_safe=False,
            )
        )
        dispatcher = attach_auto_approver(ToolDispatcher(registry))  # TD-802

        call_count = 0

        async def slow_handler(session, delay=0.5, tool_call_id=""):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(delay)
            return "Done"

        dispatcher.register_handler("slow_tool", slow_handler)

        mock = MockProvider(
            scripts={
                "test-brain": Script(
                    kind="tool_call",
                    tool_name="slow_tool",
                    tool_arguments="{}",
                    chunk_delay=0.05,
                )
            }
        )

        factory = mock_factory(mock)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, router, factory, config, tool_registry=registry, tool_dispatcher=dispatcher
            ),
        )
        await runner.start()
        await session.add_user_message("Run slow tool")

        # Wait for the tool call event to be emitted
        for _ in range(50):
            tool_events = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
            if tool_events:
                break
            await asyncio.sleep(0.02)

        # Cancel before the tool executes
        await runner.cancel()
        assert session.state == "cancelled"

        # The tool handler should not have been called
        assert call_count == 0

    async def test_cancellation_returns_no_orphaned_tasks(self) -> None:
        """After cancellation, no tasks remain in the event loop."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hello")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("Hi")
        await asyncio.sleep(0.05)
        await runner.cancel()

        # The session's runner task should be done
        assert runner.is_running is False
        # The session's task should be done
        assert runner._task is None or runner._task.done()

        # No pending tasks other than the test runner itself
        tasks = [t for t in asyncio.all_tasks() if not t.done()]
        # The only remaining task should be the test runner
        assert len(tasks) <= 1


# ── Provider-agnostic ───────────────────────────────────────────────────


class TestProviderAgnostic:
    async def test_two_mock_instances_behave_identically(self) -> None:
        """Swapping the provider (same script) yields the same events."""
        results: list[str] = []

        for _ in range(2):
            session = Session("/tmp/ws")
            router = TierRouter()
            config = make_config()
            mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Same reply")})
            runner = await start_loop(session, router, mock, config)
            await session.add_user_message("Hi")
            await wait_for_turn(session, 1)
            deltas = "".join(
                e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta)
            )
            results.append(deltas)
            await runner.cancel()

        assert results[0] == results[1] == "Same reply"

    async def test_loop_does_not_require_network(self) -> None:
        """The loop works fully offline with the mock provider."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Offline ok")})

        runner = await start_loop(session, router, mock, config)
        await session.add_user_message("ping")
        tc = await wait_for_turn(session, 1)
        assert tc.tokens > 0
        await runner.cancel()


# ── Path-scoped rule activation (TD-503) ───────────────────────────────
#
# A rule with ``appliesTo`` globs stays out of the prompt until the session
# touches a matching file; the loop then announces the activation in the
# timeline so context changes are never silent.  Raw relative tool paths
# resolve against the process CWD, so these chdir into the workspace — the
# verdict pins identically on every platform.


def _fs_dispatcher(ws: Path) -> tuple[ToolRegistry, ToolDispatcher]:
    """Real registry + builtin handlers over a tmp workspace, auto-approved."""
    registry = create_registry()
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=make_classifier(str(ws)),
            path_guard=PathGuard(Boundary(workspace_root=ws)),
            workspace=ws,
        )
    )
    register_builtin_handlers(dispatcher)
    return registry, dispatcher


def _scoped_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    (ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".tst" / "rules").mkdir(parents=True)
    (ws / ".tst" / "rules" / "src-rules.md").write_text(
        "---\nappliesTo:\n  - src/**\n---\nSRC RULES APPLY\n", encoding="utf-8"
    )
    monkeypatch.chdir(ws)
    return ws


class TestPathScopedRuleActivation:
    async def _start(self, session: Session, mock: MockProvider, ws: Path) -> SessionRunner:
        registry, dispatcher = _fs_dispatcher(ws)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(lead_turns=3),
                mock_factory(mock),
                make_config(),
                tool_registry=registry,
                tool_dispatcher=dispatcher,
            ),
        )
        await runner.start()
        return runner

    async def test_touch_activates_scoped_rule_and_announces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _scoped_workspace(tmp_path, monkeypatch)
        session = Session(str(ws))
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="fs_read",
                        tool_arguments='{"path": "src/app.py"}',
                    ),
                    Script(kind="stream", content="done"),
                ]
            }
        )
        runner = await self._start(session, mock, ws)
        await session.add_user_message("read the app file")
        await wait_for_turn(session, 1)

        activations = [e for e in session.event_log.all_events if isinstance(e, RuleActivated)]
        assert [a.rule_path for a in activations] == [".tst/rules/src-rules.md"]
        # The re-assembly after the touch carries the rule in the prompt.
        assert "SRC RULES APPLY" in (mock.calls[-1].messages[0].content or "")
        await runner.cancel()

    async def test_no_touch_means_no_activation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _scoped_workspace(tmp_path, monkeypatch)
        session = Session(str(ws))
        mock = MockProvider(
            scripts={"test-brain": Script(kind="stream", content="ok")},
        )
        runner = await self._start(session, mock, ws)
        for turn, msg in enumerate(["one", "two"], start=1):
            await session.add_user_message(msg)
            await wait_for_turn(session, turn)

        activations = [e for e in session.event_log.all_events if isinstance(e, RuleActivated)]
        assert activations == []
        # …and the scoped rule never entered the prompt.
        assert "SRC RULES APPLY" not in (mock.calls[-1].messages[0].content or "")
        await runner.cancel()

    async def test_touch_before_first_turn_sets_the_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule already active at the first assembly is the baseline, not
        an activation — nothing is announced, but the rule is in the prompt."""
        ws = _scoped_workspace(tmp_path, monkeypatch)
        session = Session(str(ws))
        session.record_touched(["src/app.py"])
        mock = MockProvider(
            scripts={"test-brain": Script(kind="stream", content="ok")},
        )
        runner = await self._start(session, mock, ws)
        await session.add_user_message("hi")
        await wait_for_turn(session, 1)

        activations = [e for e in session.event_log.all_events if isinstance(e, RuleActivated)]
        assert activations == []
        assert "SRC RULES APPLY" in (mock.calls[0].messages[0].content or "")
        await runner.cancel()


# ── Turn observability (TD-1713) ────────────────────────────────────────


class TestTurnStartedLogging:
    async def test_dequeue_logs_turn_started_with_queue_depth(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The loop logs the dequeue itself, not just the post-assembly
        "turn start" — a stall between the two was invisible (2026-08-14).
        Depth is post-dequeue: messages the loop still owes the user."""
        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hi")})

        # Enqueue before the loop starts so the first dequeue has a backlog.
        await session.add_user_message("one")
        await session.add_user_message("22")

        with caplog.at_level(logging.INFO, logger="tstd.loop"):
            runner = await start_loop(session, router, mock, config)
            await wait_for_turn(session, 2)

        started = [
            r for r in caplog.records if r.name == "tstd.loop" and r.getMessage() == "turn started"
        ]
        assert len(started) == 2
        assert started[0].session_id == session.id
        assert started[0].queued_messages == 1  # the second send still waited
        assert started[0].content_length == 3
        assert started[1].queued_messages == 0

        await runner.cancel()
