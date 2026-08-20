"""Memory block budget (TD-2502).

Loaded memory uses the TD-506 heuristic. Lowest-ranked topics drop
until under the cap. MEMORY.md is last. Dropped files keep their
selection reason so the inspector can name them and why they were
chosen before the budget cut them.
"""

from __future__ import annotations

from pathlib import Path

from tstd.context.embeddings import EmbeddingsClient, load_memory_for_turn
from tstd.context.memory_loader import (
    MemoryFile,
    MemoryLoad,
    MemoryReason,
    enforce_memory_budget,
    memory_tokens,
)
from tstd.context.tokens import heuristic_count
from tstd.memory_store import memory_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class ScriptedEmbeddings(EmbeddingsClient):
    """Canned vectors keyed by the exact input string."""

    def __init__(
        self,
        vectors: dict[str, list[float]],
        *,
        top_k: int = 4,
        token_budget: int = 2000,
    ) -> None:
        super().__init__(
            "http://unused.invalid/v1",
            "scripted",
            1,
            top_k=top_k,
            token_budget=token_budget,
        )
        self._vectors = vectors

    async def embed_or_none(self, texts: list[str]) -> list[list[float]] | None:
        out: list[list[float]] = []
        for text in texts:
            if text not in self._vectors:
                return None
            out.append(self._vectors[text])
        return out


def _file(name: str, reason: MemoryReason, text: str) -> MemoryFile:
    relative = Path(".tst") / "memory" / name
    return MemoryFile(path=relative, relative=relative, reason=reason, text=text)


class TestSameCounter:
    def test_memory_tokens_match_steering_heuristic(self) -> None:
        samples = ("", "x", "abcd", "idx\n", "x" * 80, "# Auth\n" + ("z" * 40))
        for text in samples:
            assert memory_tokens(text) == heuristic_count(text).count


class TestEnforceBudget:
    def test_lowest_ranked_topics_drop_first(self) -> None:
        index = _file("MEMORY.md", "always-index", "idx\n")
        first = _file("a.md", "heading", "a" * 40)
        last = _file("z.md", "heading", "z" * 40)
        budget = memory_tokens(index.text) + memory_tokens(first.text)
        out = enforce_memory_budget(MemoryLoad((index, first, last)), budget)
        assert out.names == ("MEMORY.md", "a.md")
        assert out.dropped_names == ("z.md",)
        assert out.dropped[0].reason == "heading"

    def test_memory_md_is_the_last_file_dropped(self) -> None:
        index = _file("MEMORY.md", "always-index", "x" * 80)
        topic = _file("auth.md", "heading", "y" * 40)
        out = enforce_memory_budget(MemoryLoad((index, topic)), token_budget=1)
        assert out.files == ()
        assert out.dropped_names == ("auth.md", "MEMORY.md")
        assert out.dropped[-1].reason == "always-index"

    def test_empty_load_stays_empty(self) -> None:
        out = enforce_memory_budget(MemoryLoad(()), token_budget=1)
        assert out.files == ()
        assert out.dropped == ()


class TestLoadRespectsBudget:
    async def test_heading_match_drops_the_lowest_topic(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        index = "idx\n"
        auth = "# Auth\n" + ("a" * 40)
        zebra = "# Auth\n" + ("z" * 40)
        _write(mem / "MEMORY.md", index)
        _write(mem / "auth.md", auth)
        _write(mem / "zebra.md", zebra)
        budget = memory_tokens(index) + memory_tokens(auth)
        load = await load_memory_for_turn(
            ws,
            "auth tokens",
            EmbeddingsClient("", "", 1, token_budget=budget),
        )
        assert load.names == ("MEMORY.md", "auth.md")
        assert load.dropped_names == ("zebra.md",)
        assert load.dropped[0].reason == "heading"

    async def test_oversize_index_is_dropped_after_topics(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "x" * 80)
        _write(mem / "auth.md", "# Auth\nshort\n")
        load = await load_memory_for_turn(
            ws,
            "auth",
            EmbeddingsClient("", "", 1, token_budget=1),
        )
        assert load.files == ()
        assert load.block is None
        assert load.dropped_names == ("auth.md", "MEMORY.md")

    async def test_rank_skip_is_visible_on_dropped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        index = "idx\n"
        huge = "# Huge\n" + ("x" * 400)
        small = "# Small\ny\n"
        _write(mem / "MEMORY.md", index)
        _write(mem / "huge.md", huge)
        _write(mem / "small.md", small)
        budget = memory_tokens(index) + memory_tokens(small)
        task = "pick"
        load = await load_memory_for_turn(
            ws,
            task,
            ScriptedEmbeddings(
                {
                    task: [1.0, 0.0],
                    huge: [0.99, 0.01],
                    small: [0.5, 0.5],
                },
                top_k=2,
                token_budget=budget,
            ),
        )
        assert load.names == ("MEMORY.md", "small.md")
        assert load.dropped_names == ("huge.md",)
        assert load.dropped[0].reason == "embedding"
