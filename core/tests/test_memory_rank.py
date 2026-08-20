"""Rank and budget for memory files (TD-2203).

Topic files are ranked by embedding similarity. MEMORY.md is always
kept. Heading-match is the tie-break and the fallback. top-k plus a
token budget come from config. Identical inputs select the same set.
"""

from __future__ import annotations

from pathlib import Path

from tstd.context.embeddings import (
    EmbeddingsClient,
    apply_rank,
    cosine_similarity,
    load_memory_for_turn,
)
from tstd.context.memory_loader import discover_memory_files
from tstd.context.tokens import heuristic_count
from tstd.memory_store import memory_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class ScriptedEmbeddings(EmbeddingsClient):
    """Returns canned vectors keyed by the exact input string."""

    def __init__(
        self,
        vectors: dict[str, list[float]],
        *,
        top_k: int = 4,
        token_budget: int = 2000,
        fail: bool = False,
    ) -> None:
        super().__init__(
            "http://unused.invalid/v1",
            "scripted",
            1,
            top_k=top_k,
            token_budget=token_budget,
        )
        self._vectors = vectors
        self._fail = fail

    async def embed_or_none(self, texts: list[str]) -> list[list[float]] | None:
        if self._fail:
            return None
        out: list[list[float]] = []
        for text in texts:
            if text not in self._vectors:
                return None
            out.append(self._vectors[text])
        return out


class TestCosine:
    def test_identical_vectors_are_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0

    def test_orthogonal_vectors_are_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_length_mismatch_is_zero(self) -> None:
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


class TestRank:
    async def test_embeddings_select_a_file_heading_match_would_miss(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        index = "durable: we pin ruff\n"
        close = "# Unrelated\nalpha alpha alpha\n"
        far = "# Auth\nrefresh tokens weekly\n"
        _write(mem / "MEMORY.md", index)
        _write(mem / "alpha.md", close)
        _write(mem / "auth.md", far)
        task = "the auth token expired"
        client = ScriptedEmbeddings(
            {
                task: [1.0, 0.0],
                close: [0.9, 0.1],
                far: [0.0, 1.0],
            },
            top_k=1,
        )
        load = await load_memory_for_turn(ws, task, client)
        assert load.names == ("MEMORY.md", "alpha.md")
        assert load.files[1].reason == "embedding"
        assert "alpha alpha" in (load.block or "")
        assert "refresh tokens" not in (load.block or "")

    async def test_failed_embed_falls_back_to_heading_match(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "index\n")
        _write(mem / "auth.md", "# Auth\nrefresh tokens weekly\n")
        _write(mem / "alpha.md", "# Unrelated\nalpha alpha\n")
        load = await load_memory_for_turn(
            ws, "the auth token expired", ScriptedEmbeddings({}, fail=True)
        )
        assert load.names == ("MEMORY.md", "auth.md")

    async def test_heading_match_wins_a_tie(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "index\n")
        heading = "# Auth\nbody\n"
        other = "# Payments\nbody\n"
        _write(mem / "zeta.md", heading)
        _write(mem / "alpha.md", other)
        same = [1.0, 0.0]
        task = "auth tokens"
        client = ScriptedEmbeddings(
            {task: same, heading: same, other: same},
            top_k=1,
        )
        load = await load_memory_for_turn(ws, task, client)
        assert load.names == ("MEMORY.md", "zeta.md")
        assert load.files[1].reason == "heading"

    async def test_top_k_caps_topics(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "index\n")
        one = "# One\none\n"
        two = "# Two\ntwo\n"
        three = "# Three\nthree\n"
        _write(mem / "a.md", one)
        _write(mem / "b.md", two)
        _write(mem / "c.md", three)
        task = "rank these"
        client = ScriptedEmbeddings(
            {
                task: [1.0, 0.0],
                one: [0.9, 0.1],
                two: [0.8, 0.2],
                three: [0.7, 0.3],
            },
            top_k=2,
        )
        load = await load_memory_for_turn(ws, task, client)
        assert load.names == ("MEMORY.md", "a.md", "b.md")

    async def test_budget_skips_a_file_that_does_not_fit(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        index = "idx\n"
        huge = "# Huge\n" + ("x" * 400)
        small = "# Small\ny\n"
        _write(mem / "MEMORY.md", index)
        _write(mem / "huge.md", huge)
        _write(mem / "small.md", small)
        budget = heuristic_count(index).count + heuristic_count(small).count
        task = "pick"
        client = ScriptedEmbeddings(
            {
                task: [1.0, 0.0],
                huge: [0.99, 0.01],
                small: [0.5, 0.5],
            },
            top_k=2,
            token_budget=budget,
        )
        load = await load_memory_for_turn(ws, task, client)
        assert load.names == ("MEMORY.md", "small.md")

    async def test_memory_md_is_always_included(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        index = "x" * 80
        topic = "# Auth\nshort\n"
        _write(mem / "MEMORY.md", index)
        _write(mem / "auth.md", topic)
        budget = heuristic_count(index).count
        task = "auth"
        client = ScriptedEmbeddings(
            {task: [1.0, 0.0], topic: [1.0, 0.0]},
            top_k=4,
            token_budget=budget,
        )
        load = await load_memory_for_turn(ws, task, client)
        assert load.names[0] == "MEMORY.md"
        assert "auth.md" not in load.names

    async def test_identical_inputs_select_the_same_set(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "index\n")
        a = "# Alpha\na\n"
        b = "# Bravo\nb\n"
        _write(mem / "a.md", a)
        _write(mem / "b.md", b)
        task = "task"
        client = ScriptedEmbeddings(
            {task: [1.0, 0.0], a: [0.8, 0.2], b: [0.4, 0.6]},
            top_k=2,
        )
        first = await load_memory_for_turn(ws, task, client)
        second = await load_memory_for_turn(ws, task, client)
        assert first.names == second.names
        assert first.block == second.block
        assert [item.reason for item in first.files] == [item.reason for item in second.files]

    def test_apply_rank_is_deterministic_for_equal_scores(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        mem = memory_dir(ws)
        _write(mem / "MEMORY.md", "index\n")
        _write(mem / "b.md", "# Bravo\n")
        _write(mem / "a.md", "# Alpha\n")
        candidates = discover_memory_files(ws)
        topics = [c for c in candidates if not c.is_index]
        task_vec = [1.0, 0.0]
        topic_vecs = [[0.5, 0.5] for _ in topics]
        first = apply_rank(
            candidates, "no overlap", task_vec, topic_vecs, top_k=2, token_budget=2000
        )
        second = apply_rank(
            candidates, "no overlap", task_vec, topic_vecs, top_k=2, token_budget=2000
        )
        assert first.names == second.names == ("MEMORY.md", "a.md", "b.md")
