"""Heading-match memory loader (TD-2201).

Session start / first brain turn loads MEMORY.md plus topic files whose
heading tokens overlap the user task. No embeddings. Empty directory
keeps the placeholder. Identical files + task are deterministic.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tests.test_loop import make_config, start_loop, wait_for_turn
from tstd.context import MEMORY_PLACEHOLDER, PromptAssembler
from tstd.context.memory_loader import heading_tokens, load_memory_for_task, tokenize
from tstd.memory_store import memory_dir
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _memory(ws: Path, name: str, content: str) -> Path:
    path = memory_dir(ws) / name
    _write(path, content)
    return path


class TestTokenize:
    def test_drops_stopwords_and_short_tokens(self) -> None:
        assert tokenize("The API is a to-do") == frozenset({"api"})

    def test_is_case_insensitive(self) -> None:
        assert tokenize("OAuth Tokens") == tokenize("oauth tokens")


class TestHeadingTokens:
    def test_reads_atx_headings(self) -> None:
        text = "# Auth\nbody\n## Refresh tokens\n"
        assert heading_tokens(text) == frozenset({"auth", "refresh", "tokens"})

    def test_ignores_headings_inside_fences(self) -> None:
        text = "# Payments\n```\n# Auth\n```\n"
        assert "auth" not in heading_tokens(text)
        assert heading_tokens(text) == frozenset({"payments"})


class TestSelect:
    def test_memory_md_always_included(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _memory(ws, "MEMORY.md", "the index has no heading\n")
        _memory(ws, "auth.md", "# Payments\nunrelated\n")
        load = load_memory_for_task(ws, "fix the auth token")
        assert load.names == ("MEMORY.md",)
        assert load.files[0].reason == "always-index"

    def test_topic_included_on_heading_overlap(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _memory(ws, "MEMORY.md", "index facts\n")
        _memory(ws, "auth.md", "# Auth\nrefresh the token\n")
        _memory(ws, "billing.md", "# Payments\ninvoice gotchas\n")
        load = load_memory_for_task(ws, "the auth token expired")
        assert load.names == ("MEMORY.md", "auth.md")
        assert load.files[1].reason == "heading"
        assert "refresh the token" in (load.block or "")
        assert "invoice" not in (load.block or "")

    def test_named_templates_are_topics_not_index(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _memory(ws, "decisions.md", "# OAuth\nwhy we picked it\n")
        _memory(ws, "gotchas.md", "# Windows\npath separators\n")
        load = load_memory_for_task(ws, "oauth refresh")
        assert load.names == ("decisions.md",)
        assert load.files[0].reason == "heading"

    def test_empty_directory_loads_nothing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        memory_dir(ws).mkdir(parents=True)
        load = load_memory_for_task(ws, "anything at all")
        assert load.files == ()
        assert load.block is None

    def test_missing_directory_loads_nothing(self, tmp_path: Path) -> None:
        load = load_memory_for_task(tmp_path / "ws", "anything at all")
        assert load.files == ()
        assert load.block is None

    def test_fenced_heading_does_not_match(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _memory(ws, "trap.md", "```\n# Auth\n```\n# Payments\n")
        load = load_memory_for_task(ws, "auth tokens")
        assert load.names == ()

    def test_steering_basename_under_memory_is_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _memory(ws, "AGENTS.md", "# Auth\nthis is a rule\n")
        _memory(ws, "SKILL.md", "---\ndescription: sneaky\n---\nmanifest\n")
        _memory(ws, "MEMORY.md", "index\n")
        load = load_memory_for_task(ws, "auth")
        assert load.names == ("MEMORY.md",)

    def test_symlink_outside_memory_is_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        outside = tmp_path / "outside.md"
        outside.write_text("# Auth\nsecret\n", encoding="utf-8")
        mem = memory_dir(ws)
        mem.mkdir(parents=True)
        (mem / "auth.md").symlink_to(outside)
        _memory(ws, "MEMORY.md", "index\n")
        load = load_memory_for_task(ws, "auth tokens")
        assert load.names == ("MEMORY.md",)
        assert "secret" not in (load.block or "")

    def test_no_embeddings_in_this_module(self) -> None:
        source = Path(__file__).parents[1] / "tstd" / "context" / "memory_loader.py"
        text = source.read_text(encoding="utf-8")
        assert "httpx" not in text
        assert "/v1/embeddings" not in text
        assert "openai" not in text.lower()


class TestPromptPlaceholder:
    def test_empty_directory_keeps_placeholder(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "root steering\n")
        memory_dir(ws).mkdir(parents=True)
        load = load_memory_for_task(ws, "fix auth")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain", memory=load.block)
        assert MEMORY_PLACEHOLDER in result.text
        assert "<!-- memory:" not in result.text.replace(MEMORY_PLACEHOLDER, "")

    def test_loaded_bytes_replace_placeholder_on_brain_only(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "root steering\n")
        _memory(ws, "MEMORY.md", "durable: we use ruff\n")
        _memory(ws, "auth.md", "# Auth\nrefresh tokens weekly\n")
        load = load_memory_for_task(ws, "auth expired")
        assembler = PromptAssembler(ws, home_dir=home)
        brain = assembler.assemble_sync("brain", memory=load.block)
        worker = assembler.assemble_sync("worker", task="auth expired", memory=load.block)
        assert MEMORY_PLACEHOLDER not in brain.text
        assert "durable: we use ruff" in brain.text
        assert "refresh tokens weekly" in brain.text
        assert "durable: we use ruff" not in worker.text
        assert MEMORY_PLACEHOLDER not in worker.text


_TOPIC_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def _random_memory_tree(base: Path, rng: random.Random) -> tuple[Path, str]:
    """Random MEMORY.md + topic files; return workspace and task."""
    ws = base / "workspace"
    _memory(ws, "MEMORY.md", f"index {rng.choice(_TOPIC_WORDS)}\n")
    chosen = rng.sample(list(_TOPIC_WORDS), k=rng.randint(2, 4))
    task_word = chosen[0]
    for i, word in enumerate(chosen):
        _memory(ws, f"topic-{i}-{word}.md", f"# {word}\nbody {word}\n")
    _memory(ws, "unrelated.md", "# zulu\nnever selected\n")
    return ws, f"please handle {task_word} today"


@pytest.mark.parametrize("seed", [20260820, 20260821, 20260822, 20260823])
def test_selection_is_deterministic(tmp_path: Path, seed: int) -> None:
    """Identical files + task select the same set twice, and on a fresh call."""
    ws, task = _random_memory_tree(tmp_path, random.Random(seed))
    first = load_memory_for_task(ws, task)
    second = load_memory_for_task(ws, task)
    third = load_memory_for_task(ws, task)
    assert first.names == second.names == third.names
    assert first.block == second.block == third.block
    assert first.names[0] == "MEMORY.md"
    assert "unrelated.md" not in first.names
    assert any(item.reason == "heading" for item in first.files)
    # Same inputs, different parent path, same relative names and reasons.
    clone = tmp_path / "clone"
    for src in memory_dir(ws).glob("*.md"):
        _write(memory_dir(clone) / src.name, src.read_text(encoding="utf-8"))
    cloned = load_memory_for_task(clone, task)
    assert cloned.names == first.names
    assert [item.reason for item in cloned.files] == [item.reason for item in first.files]


class TestFirstBrainTurn:
    async def test_matching_topic_enters_the_brain_prompt(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "root steering\n")
        _memory(ws, "MEMORY.md", "durable: we pin ruff\n")
        _memory(ws, "auth.md", "# Auth\nrefresh tokens weekly\n")
        _memory(ws, "billing.md", "# Payments\nnever log cards\n")
        session = Session(str(ws))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
        runner = await start_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message("the auth token expired")
        await wait_for_turn(session, 1)
        prompt = mock.calls[0].messages[0].content or ""
        assert "durable: we pin ruff" in prompt
        assert "refresh tokens weekly" in prompt
        assert "never log cards" not in prompt
        assert MEMORY_PLACEHOLDER not in prompt
        await runner.cancel()

    async def test_empty_memory_keeps_placeholder(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws / "AGENTS.md", "root steering\n")
        memory_dir(ws).mkdir(parents=True)
        session = Session(str(ws))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
        runner = await start_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message("the auth token expired")
        await wait_for_turn(session, 1)
        prompt = mock.calls[0].messages[0].content or ""
        assert MEMORY_PLACEHOLDER in prompt
        assert "refresh tokens" not in prompt
        await runner.cancel()
