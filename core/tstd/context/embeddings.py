"""OpenAI-compatible embeddings client and memory ranker (TD-2202, TD-2203).

Speaks ``POST /v1/embeddings`` against ``embeddings.base_url`` from
config. The host is never a Python literal. Empty ``base_url`` disables
the client. A missing or loopback-down endpoint returns ``None`` so the
caller falls back to heading-match (TD-2201) instead of failing the turn.

When the sidecar answers, topic files are ranked by cosine similarity
to the task. ``MEMORY.md`` is always tried first. Heading-match is the
tie-break and the fallback. The selected set is then cut to
``embeddings.token_budget`` (TD-506 heuristic); ``MEMORY.md`` is the
last file dropped (TD-2502).

Do not call Ollama's native embed route. A measured embed on that
scheduler evicts a resident chat model (2026-08-17).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..logging import get_logger
from .memory_loader import (
    MemoryCandidate,
    MemoryFile,
    MemoryLoad,
    MemoryReason,
    discover_memory_files,
    enforce_memory_budget,
    headings_overlap_task,
    load_memory_for_task,
    load_memory_from_global,
)
from .tokens import heuristic_count

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.embeddings")

DEFAULT_TOP_K = 4
DEFAULT_TOKEN_BUDGET = 2000


class EmbeddingsClient:
    """POST ``{base_url}/embeddings``. Never raises for a down sidecar."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        top_k: int = DEFAULT_TOP_K,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.top_k = top_k
        self.token_budget = token_budget

    @classmethod
    def from_config(cls, config: ModelConfig) -> EmbeddingsClient:
        cfg = config.embeddings
        return cls(
            cfg.base_url,
            cfg.model,
            cfg.timeout_seconds,
            top_k=cfg.top_k,
            token_budget=cfg.token_budget,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and bool(self.model)

    async def embed_or_none(self, texts: list[str]) -> list[list[float]] | None:
        """Return vectors, or ``None`` when embeddings are off or unreachable.

        Callers treat ``None`` as "use TD-2201". Operational failures are
        logged, not raised.
        """
        if not self.enabled:
            return None
        if not texts:
            return []
        url = f"{self.base_url}/embeddings"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    url,
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "embeddings unavailable; falling back to heading-match",
                extra={
                    "extra_fields": {
                        "error": str(exc),
                    }
                },
            )
            return None
        return _vectors(payload)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity. Zero vectors and length mismatch score 0."""
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def apply_rank(
    candidates: tuple[MemoryCandidate, ...],
    task: str,
    task_vec: list[float],
    topic_vecs: list[list[float]],
    *,
    top_k: int,
    token_budget: int,
) -> MemoryLoad:
    """Select ``MEMORY.md`` plus the top-k topics that fit *token_budget*.

    Topics are ordered by cosine to *task_vec*, then heading-match, then
    name. Token counts use the TD-506 heuristic on file bytes.
    """
    index: MemoryCandidate | None = None
    topics: list[MemoryCandidate] = []
    for item in candidates:
        if item.is_index:
            index = item
        else:
            topics.append(item)
    if len(topic_vecs) != len(topics):
        raise ValueError("topic vector count must match topic files")

    ranked = sorted(
        zip(topics, topic_vecs, strict=True),
        key=lambda pair: (
            -cosine_similarity(task_vec, pair[1]),
            0 if headings_overlap_task(pair[0].text, task) else 1,
            pair[0].path.name,
        ),
    )

    selected: list[MemoryFile] = []
    used = 0
    if index is not None:
        chosen = _as_file(index, "always-index")
        selected.append(chosen)
        used += heuristic_count(chosen.text).count

    taken = 0
    dropped: list[MemoryFile] = []
    for topic, _vec in ranked:
        if taken >= top_k:
            break
        reason: MemoryReason = "heading" if headings_overlap_task(topic.text, task) else "embedding"
        chosen = _as_file(topic, reason)
        cost = heuristic_count(chosen.text).count
        if used + cost > token_budget:
            dropped.append(chosen)
            continue
        selected.append(chosen)
        used += cost
        taken += 1
    return MemoryLoad(tuple(selected), dropped=tuple(dropped))


def _as_file(candidate: MemoryCandidate, reason: MemoryReason) -> MemoryFile:
    return MemoryFile(
        path=candidate.path,
        relative=candidate.relative,
        reason=reason,
        text=candidate.text,
    )


async def load_memory_for_turn(
    workspace: str | Path,
    task: str,
    client: EmbeddingsClient | None = None,
    *,
    load_global: bool = False,
    home: Path | None = None,
) -> MemoryLoad:
    """Rank when the sidecar answers; otherwise heading-match.

    A down sidecar cannot fail the turn. Global files are appended only
    when *load_global* is on — off never stats ``~/.tstdesk/memory``.
    """
    heading = load_memory_for_task(workspace, task)
    budget = client.token_budget if client is not None else DEFAULT_TOKEN_BUDGET
    if client is None or not client.enabled:
        result = enforce_memory_budget(heading, budget)
    else:
        candidates = discover_memory_files(workspace)
        topics = [item for item in candidates if not item.is_index]
        if not topics:
            result = enforce_memory_budget(heading, budget)
        else:
            vectors = await client.embed_or_none([task, *[item.text for item in topics]])
            if vectors is None or len(vectors) != 1 + len(topics):
                result = enforce_memory_budget(heading, budget)
            else:
                ranked = apply_rank(
                    candidates,
                    task,
                    vectors[0],
                    vectors[1:],
                    top_k=client.top_k,
                    token_budget=client.token_budget,
                )
                result = enforce_memory_budget(ranked, client.token_budget)
    if not load_global:
        return result
    extra = load_memory_from_global(home if home is not None else Path.home(), task)
    merged = MemoryLoad(result.files + extra.files, dropped=result.dropped + extra.dropped)
    return enforce_memory_budget(merged, budget)


def _vectors(payload: object) -> list[list[float]] | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    parsed: list[tuple[int, list[float]]] = []
    for offset, row in enumerate(rows):
        if not isinstance(row, dict):
            return None
        embedding = row.get("embedding")
        if not isinstance(embedding, list) or not all(
            isinstance(value, int | float) for value in embedding
        ):
            return None
        raw_index = row.get("index", offset)
        if not isinstance(raw_index, int):
            return None
        parsed.append((raw_index, [float(value) for value in embedding]))
    parsed.sort(key=lambda item: item[0])
    return [vector for _, vector in parsed]
