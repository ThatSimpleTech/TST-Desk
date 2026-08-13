"""Cost and token accounting (TD-304).

Tracks every API call, computes cost from tier pricing, and aggregates
per turn, per session, and per day. Cache-read tokens are priced at the
cache rate, never the input rate.
"""

from __future__ import annotations

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
        self._session_start = datetime.now()

    # ── Recording ────────────────────────────────────────────────────

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
        self._calls.append(record)
        self._turn_calls.append(record)
        return cost

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
        }
