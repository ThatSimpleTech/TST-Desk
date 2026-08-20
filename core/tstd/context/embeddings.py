"""OpenAI-compatible embeddings client (TD-2202).

Speaks ``POST /v1/embeddings`` against ``embeddings.base_url`` from
config. The host is never a Python literal. Empty ``base_url`` disables
the client. A missing or loopback-down endpoint returns ``None`` so the
caller falls back to heading-match (TD-2201) instead of failing the turn.

Do not call Ollama's native embed route. A measured embed on that
scheduler evicts a resident chat model (2026-08-17).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..logging import get_logger
from .memory_loader import MemoryLoad, load_memory_for_task

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.embeddings")


class EmbeddingsClient:
    """POST ``{base_url}/embeddings``. Never raises for a down sidecar."""

    def __init__(self, base_url: str, model: str, timeout_seconds: float) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_config(cls, config: ModelConfig) -> EmbeddingsClient:
        cfg = config.embeddings
        return cls(cfg.base_url, cfg.model, cfg.timeout_seconds)

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


def _vectors(payload: object) -> list[list[float]] | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    out: list[list[float]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        embedding = row.get("embedding")
        if not isinstance(embedding, list) or not all(
            isinstance(value, int | float) for value in embedding
        ):
            return None
        out.append([float(value) for value in embedding])
    return out


async def load_memory_for_turn(
    workspace: str | Path,
    task: str,
    client: EmbeddingsClient | None = None,
) -> MemoryLoad:
    """Heading-match load; a down sidecar cannot fail the turn.

    Ranking on a live sidecar is TD-2203. This story only probes so a
    configured-but-dead endpoint is a fallback, not an exception.
    """
    heading = load_memory_for_task(workspace, task)
    if client is None or not client.enabled:
        return heading
    await client.embed_or_none([task])
    return heading
