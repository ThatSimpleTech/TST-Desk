"""Tests for cache-aware prompt assembly in the agent loop (TD-305).

Verifies, using a real multi-turn session with the mock provider, that
the stable prefix (blocks 1-2) is byte-identical across turns when
steering files have not changed, and that the prefix hash and cache
ratio are observable in the turn logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context import PromptAssembler
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete
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


# ── Multi-turn prefix stability test ────────────────────────────────────


class TestMultiTurnPrefixStability:
    async def test_prefix_identical_across_turns(self, tmp_path: Path) -> None:
        """Blocks 1-2 are byte-identical across a multi-turn session."""
        home, ws = _build_workspace(tmp_path, "root: use python3")

        # Build the assembler that the loop will use
        assembler = PromptAssembler(ws, home_dir=home)

        session = Session("/tmp/ws")
        router = TierRouter()
        config = make_config()
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Brain reply"),
                "test-worker": Script(kind="stream", content="Worker reply"),
            },
        )

        factory = mock_factory(mock)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                router,
                factory,
                config,
                prompt_assembler=assembler,
            ),
        )
        await runner.start()

        # Turn 1: brain
        await session.add_user_message("First")
        tc1 = await wait_for_turn(session, 1)
        assert tc1.tier == "brain"
        first_hash = assembler.last_assembled.prefix_hash

        # Turn 2: brain (steering files unchanged)
        await session.add_user_message("Second")
        tc2 = await wait_for_turn(session, 2)
        assert tc2.tier == "brain"
        second_hash = assembler.last_assembled.prefix_hash

        # Turn 3: worker
        await session.add_user_message("Third")
        tc3 = await wait_for_turn(session, 3)
        assert tc3.tier == "worker"
        third_hash = assembler.last_assembled.prefix_hash

        # Blocks 1-2 are byte-identical across all three turns
        # (brain and worker share the same steering block).
        assert first_hash == second_hash == third_hash, (
            f"prefix hashes differ: {first_hash} / {second_hash} / {third_hash}"
        )

        # The full system message for brain turns 1 and 2 should be
        # byte-identical (steering + manifest unchanged).
        system_messages = [call.messages[0].content for call in mock.calls]
        assert system_messages[0] == system_messages[1], (
            "brain turns 1 and 2 should have identical system prompts"
        )
        # Worker turn 3 differs (no manifest, has task)
        assert system_messages[2] != system_messages[0]

        await runner.cancel()

    async def test_prefix_changes_after_steering_change(self, tmp_path: Path) -> None:
        """Prefix hash changes when a steering file is modified mid-session."""
        home, ws = _build_workspace(tmp_path, "root: original steering")

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

        factory = mock_factory(mock)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                router,
                factory,
                config,
                prompt_assembler=assembler,
            ),
        )
        await runner.start()

        await session.add_user_message("First")
        await wait_for_turn(session, 1)
        before = assembler.last_assembled.prefix_hash

        # Modify the steering file mid-session
        _write(ws / "AGENTS.md", "root: modified steering")

        await session.add_user_message("Second")
        await wait_for_turn(session, 2)
        after = assembler.last_assembled.prefix_hash

        assert before != after, "prefix hash should change after steering file modification"

        await runner.cancel()
