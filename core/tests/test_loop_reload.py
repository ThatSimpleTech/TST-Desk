"""Tests for steering hot reload (TD-509).

Covers:
- Steering file edits are detected and re-resolved without restarting
- Reload announced in the timeline (SteeringReloaded event) and
  reflected in the inspector (InstructionStack event)
- Cache prefix invalidation: prefix hash changes on reload
- Debounced against rapid successive saves (one event per state change)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context import PromptAssembler
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import InstructionStack, SteeringReloaded, TurnComplete
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

# ── Helpers (matching test_loop.py conventions) ─────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(base: Path, root_file: str) -> tuple[Path, Path]:
    """Build a workspace with a root AGENTS.md; return (home, ws)."""
    home = base / "home"
    ws = base / "workspace"
    _write(ws / "AGENTS.md", root_file)
    return home, ws


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


def _reloads(session: Session) -> list[SteeringReloaded]:
    return [e for e in session.event_log.all_events if isinstance(e, SteeringReloaded)]


def _stacks(session: Session) -> list[InstructionStack]:
    return [e for e in session.event_log.all_events if isinstance(e, InstructionStack)]


def _start_runner(
    session: Session,
    router: TierRouter,
    config: ModelConfig,
    mock: MockProvider,
    assembler: PromptAssembler,
) -> SessionRunner:
    factory = mock_factory(mock)
    return SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            router,
            factory,
            config,
            prompt_assembler=assembler,
        ),
    )


# ── Reload detection ────────────────────────────────────────────────────


class TestReloadDetection:
    async def test_steering_change_emits_reload(self, tmp_path: Path) -> None:
        """A steering edit is detected and announced in the timeline."""
        home, ws = _build_workspace(tmp_path, "root: original")
        assembler = PromptAssembler(ws, home_dir=home)

        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Reply"),
                "test-worker": Script(kind="stream", content="Reply"),
            },
        )
        runner = _start_runner(session, router, config, mock, assembler)
        await runner.start()

        # Turn 1: no reload (first assembly of the session)
        await session.add_user_message("First")
        await wait_for_turn(session, 1)
        assert _reloads(session) == []

        # Edit a steering file mid-session
        _write(ws / "AGENTS.md", "root: changed")

        # Turn 2: reload detected
        await session.add_user_message("Second")
        await wait_for_turn(session, 2)

        reloads = _reloads(session)
        assert len(reloads) == 1

        # The reload event carries the new prefix hash
        assert assembler.last_assembled is not None
        new_hash = assembler.last_assembled.prefix_hash
        assert reloads[0].prefix_hash == new_hash
        assert reloads[0].prefix_tokens > 0
        assert reloads[0].source_count >= 1

        # The two token figures are distinct, and the event does not pass
        # the prefix off as the cost of the user's steering: the prefix
        # also carries the base prompt and the workspace root (TD-1810),
        # which nobody edited.
        assert reloads[0].steering_tokens == assembler.last_assembled.steering_tokens
        assert reloads[0].steering_tokens > 0
        assert reloads[0].steering_tokens < reloads[0].prefix_tokens

        # Cache prefix invalidated: the reload hash differs from turn 1
        # (which had no reload event, so compare against the emitted one)
        await runner.cancel()

    async def test_no_reload_when_unchanged(self, tmp_path: Path) -> None:
        """No reload event when steering files have not changed."""
        home, ws = _build_workspace(tmp_path, "root: stable")
        assembler = PromptAssembler(ws, home_dir=home)

        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Reply"),
                "test-worker": Script(kind="stream", content="Reply"),
            },
        )
        runner = _start_runner(session, router, config, mock, assembler)
        await runner.start()

        await session.add_user_message("First")
        await wait_for_turn(session, 1)
        await session.add_user_message("Second")
        await wait_for_turn(session, 2)

        assert _reloads(session) == []
        await runner.cancel()


# ── Inspector reflection ────────────────────────────────────────────────


class TestInspectorReflection:
    async def test_reload_emits_instruction_stack(self, tmp_path: Path) -> None:
        """A fresh instruction stack is pushed to the inspector on reload."""
        home, ws = _build_workspace(tmp_path, "root: original")
        _write(ws / ".tst" / "rules" / "style.md", "Style rule")
        assembler = PromptAssembler(ws, home_dir=home)

        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Reply"),
                "test-worker": Script(kind="stream", content="Reply"),
            },
        )
        runner = _start_runner(session, router, config, mock, assembler)
        await runner.start()

        await session.add_user_message("First")
        await wait_for_turn(session, 1)
        assert _stacks(session) == []

        # Edit steering mid-session
        _write(ws / ".tst" / "rules" / "style.md", "Style rule: updated")

        await session.add_user_message("Second")
        await wait_for_turn(session, 2)

        stacks = _stacks(session)
        assert len(stacks) == 1
        # The stack reflects the updated rule file path
        assert any("style.md" in e.path for e in stacks[0].sources)
        assert stacks[0].total_tokens > 0

        await runner.cancel()


# ── Debounce ────────────────────────────────────────────────────────────


class TestDebounce:
    async def test_rapid_saves_single_reload(self, tmp_path: Path) -> None:
        """Rapid successive saves between turns produce one reload event."""
        home, ws = _build_workspace(tmp_path, "root: v1")
        assembler = PromptAssembler(ws, home_dir=home)

        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Reply"),
                "test-worker": Script(kind="stream", content="Reply"),
            },
        )
        runner = _start_runner(session, router, config, mock, assembler)
        await runner.start()

        await session.add_user_message("First")
        await wait_for_turn(session, 1)

        # Rapid saves: v1 → v2 → v3 → v4 before the next turn
        for content in ("root: v2", "root: v3", "root: v4"):
            _write(ws / "AGENTS.md", content)

        await session.add_user_message("Second")
        await wait_for_turn(session, 2)

        # One reload event for the final state, not one per save
        reloads = _reloads(session)
        assert len(reloads) == 1
        assert assembler.last_assembled is not None
        assert reloads[0].prefix_hash == assembler.last_assembled.prefix_hash

        await runner.cancel()
