"""Token counting for steering sources (TD-506).

Per-source token counts are computed with the tier's tokenizer where
available, or a documented approximation whose method is carried in the
count itself so the UI can state it (spec §4.4).

Available tokenizers:

* ``HeuristicTokenCounter`` — always available.  Approximation:
  ``tokens ≈ ceil(chars / 4)``, a widely used rule of thumb for English
  text.  The method string "approximation (4 chars/token)" is carried
  on every count.
* ``TiktokenTokenCounter`` — used when ``tiktoken`` is installed and the
  model slug maps to a known OpenAI encoding.  Falls back to the
  heuristic (with the approximation method stated) for unknown models.

``make_token_counter()`` picks the best available counter for a model
slug.  The daemon passes the active tier's slug (TD-305); tests and the
assembler default use the heuristic, which is deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

APPROX_METHOD = "approximation (4 chars/token)"


@dataclass(frozen=True)
class TokenCount:
    """A token count with the method used to compute it.

    Attributes:
        count: Estimated or exact token count.
        method: Human-readable description of the counting method, for
            display in the inspector/UI (spec §4.4).
    """

    count: int
    method: str


class TokenCounter(Protocol):
    """Counts tokens in text."""

    def count(self, text: str) -> TokenCount:
        """Return the token count for *text* with its method."""
        ...


def heuristic_count(text: str) -> TokenCount:
    """Approximate tokens as ``ceil(chars / 4)``.

    A documented rule-of-thumb approximation for English text.  Not
    accurate for code-heavy or non-English content — the approximation
    method is stated on the count so consumers can discount it.
    """
    return TokenCount(count=max(1, math.ceil(len(text) / 4)), method=APPROX_METHOD)


class HeuristicTokenCounter:
    """Always-available approximate token counter."""

    def count(self, text: str) -> TokenCount:
        return heuristic_count(text)


def _load_tiktoken() -> Any | None:
    """Import tiktoken lazily; return None when it is not installed."""
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken
    except ImportError:
        return None


class TiktokenTokenCounter:
    """Token counter using tiktoken when the model is known.

    Falls back to the heuristic approximation for model slugs tiktoken
    does not recognise (BYO models, local endpoints, unknown OpenAI
    models) — the fallback method is stated on the count.
    """

    def __init__(self, model_slug: str) -> None:
        self._model_slug = model_slug
        self._encoding: Any | None = self._load_encoding()

    def _load_encoding(self) -> Any | None:
        tk = _load_tiktoken()
        if tk is None:
            return None
        try:
            return tk.encoding_for_model(self._model_slug)
        except Exception:
            return None  # unknown model → fall back to heuristic

    def count(self, text: str) -> TokenCount:
        encoding = self._encoding
        if encoding is None:
            return heuristic_count(text)
        return TokenCount(count=len(encoding.encode(text)), method=f"tiktoken:{encoding.name}")


def make_token_counter(model_slug: str | None = None) -> TokenCounter:
    """Pick the best available token counter for *model_slug*.

    * ``None`` (no tier selected) → heuristic.
    * Known model slug with tiktoken installed → tiktoken encoding.
    * Unknown slug → heuristic (tiktoken falls back internally).
    """
    if model_slug:
        return TiktokenTokenCounter(model_slug)
    return HeuristicTokenCounter()
