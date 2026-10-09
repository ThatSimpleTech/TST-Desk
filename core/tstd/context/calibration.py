"""Token estimates calibrated from provider usage (TD-5007).

The heuristic counter (4 chars/token) runs low against what a provider
reports: code and JSON tokenize denser than English prose. On EZER's
local model the estimate was 1.55-1.6x under ``usage.prompt_tokens``,
so compaction and in-turn elision aimed at a budget the real prompt had
already passed.

The fix is to learn the ratio instead of guessing it. Before each main
turn call the loop takes the base (uncalibrated) estimate of exactly the
messages and tools it sends. When the provider reports
``usage.prompt_tokens`` for that call, the ratio between the two is
folded into a per-endpoint store. Every later estimate for that endpoint
is scaled by it.

The store is process-wide on purpose: sessions in one daemon talk to
the same endpoints, and a ratio learned by one is true for all of them.
A delegate child does not estimate at all; it inherits only the
parent's tool-result cap. The store lives in memory only. After a daemon restart
the first call is estimated at ratio 1.0 (plus tool accounting), and the
context-overflow retry still backstops it.
"""

from __future__ import annotations

import math

from .tokens import CHARS_PER_TOKEN, TokenCount, TokenCounter

# The first observation replaces the seed outright: the seed is a guess,
# the observation is a measurement. Later ones move it by this much.
EMA_ALPHA = 0.3
# A ratio outside this band is a provider reporting something other than
# this prompt's size (a cached template, a different tokenizer bug), not
# a tokenizer difference. Each sample is clamped before it is folded in.
MIN_RATIO = 0.5
MAX_RATIO = 3.0
# Below this the fixed per-request overhead (BOS, template preamble)
# dominates the ratio and would inflate every later estimate.
MIN_RAW_TOKENS = 256

CalibrationKey = tuple[str, str]
"""``(base_url, model slug)`` — one tokenizer behind one endpoint."""


def _clamp(ratio: float) -> float:
    return min(MAX_RATIO, max(MIN_RATIO, ratio))


class CalibrationStore:
    """Learned ``prompt_tokens / raw estimate`` ratios per endpoint."""

    def __init__(self) -> None:
        self._ratios: dict[CalibrationKey, float] = {}

    def ratio(self, key: CalibrationKey) -> float | None:
        """The learned ratio for *key*, or ``None`` before any observation."""
        return self._ratios.get(key)

    def observe(
        self, key: CalibrationKey, raw_estimate: int, prompt_tokens: int | None
    ) -> float | None:
        """Fold one call's usage into *key*'s ratio; return the new ratio.

        ``raw_estimate`` must come from the base counter. Feeding it a
        calibrated estimate would compound the ratio on every call.
        Returns ``None`` (and changes nothing) when the observation is
        ignored: no usage figure, or an estimate too small to trust.
        """
        if prompt_tokens is None or prompt_tokens <= 0 or raw_estimate < MIN_RAW_TOKENS:
            return None
        sample = _clamp(prompt_tokens / raw_estimate)
        previous = self._ratios.get(key)
        updated = sample if previous is None else previous + EMA_ALPHA * (sample - previous)
        self._ratios[key] = updated
        return updated

    def clear(self) -> None:
        """Forget every ratio. Tests call this; the daemon never does."""
        self._ratios.clear()


_PROCESS_STORE = CalibrationStore()


def process_calibration_store() -> CalibrationStore:
    """The daemon-wide store every loop shares unless one is injected."""
    return _PROCESS_STORE


class CalibratedTokenCounter:
    """A :class:`TokenCounter` scaled by the endpoint's learned ratio.

    Uncalibrated, it counts exactly like its base and states the base's
    method. Calibrated, each count is ``ceil(base * ratio)`` and the
    method names the ratio, so a ContextCompacted log line says which
    numbers it believed.
    """

    def __init__(self, base: TokenCounter, store: CalibrationStore, key: CalibrationKey) -> None:
        self._base = base
        self._store = store
        self._key = key

    @property
    def base(self) -> TokenCounter:
        """The uncalibrated counter. Raw estimates for observation use it."""
        return self._base

    @property
    def store(self) -> CalibrationStore:
        return self._store

    @property
    def key(self) -> CalibrationKey:
        return self._key

    @property
    def ratio(self) -> float | None:
        return self._store.ratio(self._key)

    def count(self, text: str) -> TokenCount:
        raw = self._base.count(text)
        ratio = self.ratio
        if ratio is None:
            return raw
        return TokenCount(
            count=math.ceil(raw.count * ratio),
            method=f"{raw.method} x{ratio:.2f} calibrated from provider usage",
        )

    def observe(self, raw_estimate: int, prompt_tokens: int | None) -> float | None:
        """Record one call's usage against this counter's endpoint."""
        return self._store.observe(self._key, raw_estimate, prompt_tokens)


def calibration_ratio(counter: TokenCounter) -> float:
    """The scale *counter* applies on top of its base; 1.0 when none."""
    if isinstance(counter, CalibratedTokenCounter):
        ratio = counter.ratio
        if ratio is not None:
            return ratio
    return 1.0


def scale_tokens(counter: TokenCounter, tokens: int) -> int:
    """Scale a token figure that did not come from ``counter.count``.

    Per-message overhead is a token constant, not text, so it never
    passes through the counter. It is part of the raw estimate the
    ratio was learned against, so it has to be scaled the same way.
    """
    ratio = calibration_ratio(counter)
    if ratio == 1.0:
        return tokens
    return math.ceil(tokens * ratio)


def effective_chars_per_token(counter: TokenCounter) -> float:
    """Characters one estimated token stands for under *counter*.

    Elision and the tool-result cap turn a token budget back into
    characters. With a ratio of 1.5, one calibrated token is 4 / 1.5
    characters of text, so converting with a flat 4 would cut too little.
    """
    return CHARS_PER_TOKEN / calibration_ratio(counter)
