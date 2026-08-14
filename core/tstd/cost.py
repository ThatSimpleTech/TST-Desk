"""Cost and token accounting (TD-304).

Tracks every API call, computes cost from tier pricing, and aggregates
per turn, per session, and per day. Cache-read tokens are priced at the
cache rate, never the input rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ModelConfig, TierConfig
    from .protocol import CostUpdate
    from .provider import Usage
    from .router import TierName


@dataclass
class CallRecord:
    """A single API call with token usage and computed cost."""

    tier: str
    model: str
    prompt_tokens: int
    cached_prompt_tokens: int
    completion_tokens: int
    uncached_prompt_tokens: int
    prompt_cost: float
    cached_cost: float
    completion_cost: float
    cost: float
    timestamp: datetime = field(default_factory=datetime.now)


# ── Cost computation ──────────────────────────────────────────────────


def compute_call_cost(usage: Usage, tier_cfg: TierConfig) -> float:
    """Compute the dollar cost of a single API call.

    Args:
        usage: Token usage returned by the API.
        tier_cfg: Pricing for the tier used.

    Returns:
        Cost in dollars.
    """
    uncached = max(0, usage.prompt_tokens - usage.cached_prompt_tokens)
    prompt_cost = uncached * tier_cfg.input_price / 1_000_000
    cached_cost = usage.cached_prompt_tokens * tier_cfg.cache_read_price / 1_000_000
    completion_cost = usage.completion_tokens * tier_cfg.output_price / 1_000_000

    return round(prompt_cost + cached_cost + completion_cost, 6)


def compute_call_details(usage: Usage, tier_cfg: TierConfig) -> dict[str, float]:
    """Return a breakdown of cost components for a call.

    Returns:
        Dict with keys: uncached_prompt_tokens, prompt_cost, cached_cost,
        completion_cost, total_cost.
    """
    uncached = max(0, usage.prompt_tokens - usage.cached_prompt_tokens)
    return {
        "uncached_prompt_tokens": uncached,
        "prompt_cost": round(uncached * tier_cfg.input_price / 1_000_000, 6),
        "cached_cost": round(usage.cached_prompt_tokens * tier_cfg.cache_read_price / 1_000_000, 6),
        "completion_cost": round(usage.completion_tokens * tier_cfg.output_price / 1_000_000, 6),
        "total_cost": round(
            uncached * tier_cfg.input_price / 1_000_000
            + usage.cached_prompt_tokens * tier_cfg.cache_read_price / 1_000_000
            + usage.completion_tokens * tier_cfg.output_price / 1_000_000,
            6,
        ),
    }


# ── Cost tracker ──────────────────────────────────────────────────────


class CostTracker:
    """Per-session cost tracker.

    Records every API call, computes cost from tier pricing, and
    aggregates per turn, per session, and per day.

    Usage::

        tracker = CostTracker(config)
        tracker.begin_turn()
        cost = tracker.record("brain", usage, config.tier("brain"))
        update = tracker.emit_cost_update(session_id="...")
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._calls: list[CallRecord] = []
        self._turn_calls: list[CallRecord] = []
        self._classifier_calls: list[CallRecord] = []
        self._session_start = datetime.now()
        self._listeners: list[Callable[[CallRecord, bool], None]] = []

    # ── Recording ────────────────────────────────────────────────────

    def add_listener(self, listener: Callable[[CallRecord, bool], None]) -> None:
        """Register a sink called with ``(record, is_classifier)`` after
        every recorded call.

        This is the audit trail's feed (TD-902): every model call passes
        through this tracker, so one listener here guarantees the audit
        store sees them all without the loop knowing the store exists.
        Listeners must be fast and non-blocking — they run inside the
        agent loop.
        """
        self._listeners.append(listener)

    def _notify(self, record: CallRecord, is_classifier: bool) -> None:
        for listener in self._listeners:
            listener(record, is_classifier)

    def begin_turn(self) -> None:
        """Start a new turn. Resets the turn-level accumulator."""
        self._turn_calls = []

    def record(self, tier: TierName, usage: Usage, cfg: TierConfig | None = None) -> float:
        """Record an API call and return its dollar cost.

        Args:
            tier: The tier that handled the call.
            usage: Token usage from the API response.
            cfg: The tier config with pricing. If ``None``, looked up
                from the model config's active preset.

        Returns:
            The computed cost of this call in dollars.
        """
        record, cost = self._build_record(tier, usage, cfg)
        self._calls.append(record)
        self._turn_calls.append(record)
        self._notify(record, is_classifier=False)
        return cost

    def record_classifier(
        self,
        tier: TierName,
        usage: Usage,
        cfg: TierConfig | None = None,
    ) -> float:
        """Record a decision-classifier API call, tracked separately.

        Classifier calls (TD-703) are accounted apart from the turn/session
        totals so the ambiguity fallback is visible on its own breakdown
        line rather than folded into main-loop cost.
        """
        record, cost = self._build_record(tier, usage, cfg)
        self._classifier_calls.append(record)
        self._notify(record, is_classifier=True)
        return cost

    def _build_record(
        self,
        tier: TierName,
        usage: Usage,
        cfg: TierConfig | None = None,
    ) -> tuple[CallRecord, float]:
        """Build a CallRecord for *usage* and compute its dollar cost."""
        tier_cfg = cfg or self._config.tier(tier)
        cost = compute_call_cost(usage, tier_cfg)
        uncached = max(0, usage.prompt_tokens - usage.cached_prompt_tokens)
        prompt_cost = round(uncached * tier_cfg.input_price / 1_000_000, 6)
        cached_cost = round(usage.cached_prompt_tokens * tier_cfg.cache_read_price / 1_000_000, 6)
        completion_cost = round(usage.completion_tokens * tier_cfg.output_price / 1_000_000, 6)

        record = CallRecord(
            tier=tier,
            model=tier_cfg.slug,
            prompt_tokens=usage.prompt_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            completion_tokens=usage.completion_tokens,
            uncached_prompt_tokens=uncached,
            prompt_cost=prompt_cost,
            cached_cost=cached_cost,
            completion_cost=completion_cost,
            cost=cost,
        )
        return record, cost

    # ── Aggregation ──────────────────────────────────────────────────

    def turn_cost(self) -> float:
        """Total cost of the current turn (dollars)."""
        return round(sum(c.cost for c in self._turn_calls), 6)

    def turn_tokens(self) -> int:
        """Total tokens consumed in the current turn."""
        return sum(c.prompt_tokens + c.completion_tokens for c in self._turn_calls)

    def turn_cached_tokens(self) -> int:
        """Total cached prompt tokens in the current turn."""
        return sum(c.cached_prompt_tokens for c in self._turn_calls)

    def turn_uncached_tokens(self) -> int:
        """Total uncached prompt tokens in the current turn."""
        return sum(c.uncached_prompt_tokens for c in self._turn_calls)

    def turn_cache_ratio(self) -> float:
        """Cache hit ratio for the current turn (0.0 to 1.0).

        Returns 0.0 if no prompt tokens were consumed this turn.
        """
        total = sum(c.prompt_tokens for c in self._turn_calls)
        cached = sum(c.cached_prompt_tokens for c in self._turn_calls)
        return cached / total if total > 0 else 0.0

    def session_cost(self) -> float:
        """Total cost of this session (dollars)."""
        return round(sum(c.cost for c in self._calls), 6)

    def cost_by_tier(self) -> dict[str, float]:
        """Session spend per tier (dollars), tiers with spend only (TD-1006).

        Classifier calls are excluded — they surface via
        :meth:`classifier_cost`.
        """
        by_tier: dict[str, float] = {}
        for c in self._calls:
            by_tier[c.tier] = by_tier.get(c.tier, 0.0) + c.cost
        return {tier: round(cost, 6) for tier, cost in by_tier.items()}

    def session_tokens(self) -> int:
        """Total tokens consumed in this session."""
        return sum(c.prompt_tokens + c.completion_tokens for c in self._calls)

    def day_cost(self) -> float:
        """Total cost of calls made today (dollars)."""
        today = date.today()
        return round(
            sum(c.cost for c in self._calls if c.timestamp.date() == today),
            6,
        )

    def day_tokens(self) -> int:
        """Total tokens consumed today."""
        today = date.today()
        return sum(
            c.prompt_tokens + c.completion_tokens
            for c in self._calls
            if c.timestamp.date() == today
        )

    # ── Classifier cost (TD-703) ─────────────────────────────────────

    def classifier_cost(self) -> float:
        """Total cost of decision-classifier worker calls (dollars)."""
        return round(sum(c.cost for c in self._classifier_calls), 6)

    def classifier_call_count(self) -> int:
        """Number of decision-classifier worker calls made."""
        return len(self._classifier_calls)

    # ── Emission ─────────────────────────────────────────────────────

    def emit_cost_update(self, session_id: str) -> CostUpdate:
        """Build a ``CostUpdate`` event for the current state.

        Args:
            session_id: The session to stamp on the event.

        Returns:
            A ``CostUpdate`` event with turn, session, and day costs.
        """
        from .protocol import CostUpdate

        return CostUpdate(
            session_id=session_id,
            turn_cost=self.turn_cost(),
            session_cost=self.session_cost(),
            total_cost=self.day_cost(),
            classifier_cost=self.classifier_cost(),
            cost_by_tier=self.cost_by_tier(),
            seq=1,  # overwritten by the event log
        )

    # ── Inspection ───────────────────────────────────────────────────

    @property
    def calls(self) -> list[CallRecord]:
        """All calls recorded by this tracker."""
        return list(self._calls)

    def summary(self) -> dict[str, float | int]:
        """Return a snapshot of the tracker's state."""
        return {
            "turn_cost": self.turn_cost(),
            "turn_tokens": self.turn_tokens(),
            "turn_cache_ratio": round(self.turn_cache_ratio(), 4),
            "session_cost": self.session_cost(),
            "session_tokens": self.session_tokens(),
            "day_cost": self.day_cost(),
            "day_tokens": self.day_tokens(),
            "call_count": len(self._calls),
            "classifier_cost": self.classifier_cost(),
            "classifier_call_count": self.classifier_call_count(),
        }
