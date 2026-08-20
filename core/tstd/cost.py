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
    """A single API call with token usage and computed cost.

    ``cached_prompt_tokens`` carries the provider's own figure, or ``None``
    when the response reported none (TD-1811).  It is never inferred from
    the prompt's shape — a stable prefix is a reason to *expect* reuse, not
    evidence that any happened.
    """

    tier: str
    model: str
    prompt_tokens: int
    cached_prompt_tokens: int | None
    completion_tokens: int
    uncached_prompt_tokens: int
    prompt_cost: float
    cached_cost: float
    completion_cost: float
    cost: float
    timestamp: datetime = field(default_factory=datetime.now)


# ── Cost computation ──────────────────────────────────────────────────


def billable_cached_tokens(cached: int | None) -> int:
    """Cached tokens to price, given what the provider reported (TD-1811).

    An unreported figure (``None``) bills as zero cached tokens — the whole
    prompt at the input rate.  That is the conservative direction: a
    provider that stays silent about reuse is one whose invoice we cannot
    assume was discounted, so the meter must not quietly under-state spend.
    It is a *pricing and ledger-storage* fallback only.  The audit column
    is an integer sum of billed reuse, so silence stores as ``0``.
    Nothing that reports cache *state* to the user (the turn log's
    ``cache_reported``, the stack badge,
    :attr:`CostTracker.last_cached_prompt_tokens`) may route through here.
    """
    return cached if cached is not None else 0


def compute_call_cost(usage: Usage, tier_cfg: TierConfig) -> float:
    """Compute the dollar cost of a single API call.

    Args:
        usage: Token usage returned by the API.
        tier_cfg: Pricing for the tier used.

    Returns:
        Cost in dollars.
    """
    cached_tokens = billable_cached_tokens(usage.cached_prompt_tokens)
    uncached = max(0, usage.prompt_tokens - cached_tokens)
    prompt_cost = uncached * tier_cfg.input_price / 1_000_000
    cached_cost = cached_tokens * tier_cfg.cache_read_price / 1_000_000
    completion_cost = usage.completion_tokens * tier_cfg.output_price / 1_000_000

    return round(prompt_cost + cached_cost + completion_cost, 6)


def compute_call_details(usage: Usage, tier_cfg: TierConfig) -> dict[str, float]:
    """Return a breakdown of cost components for a call.

    Returns:
        Dict with keys: uncached_prompt_tokens, prompt_cost, cached_cost,
        completion_cost, total_cost.
    """
    cached_tokens = billable_cached_tokens(usage.cached_prompt_tokens)
    uncached = max(0, usage.prompt_tokens - cached_tokens)
    return {
        "uncached_prompt_tokens": uncached,
        "prompt_cost": round(uncached * tier_cfg.input_price / 1_000_000, 6),
        "cached_cost": round(cached_tokens * tier_cfg.cache_read_price / 1_000_000, 6),
        "completion_cost": round(usage.completion_tokens * tier_cfg.output_price / 1_000_000, 6),
        "total_cost": round(
            uncached * tier_cfg.input_price / 1_000_000
            + cached_tokens * tier_cfg.cache_read_price / 1_000_000
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
        """Start a new turn. Resets the turn-level accumulator.

        Called once per *turn*, never per provider call: a turn that makes
        a tool call spends several calls, and resetting between them makes
        every ``turn_*`` aggregate report the last leg (TD-1806).  Only
        ``_turn_calls`` is reset — the session ledger, the classifier
        ledger and :attr:`last_cached_prompt_tokens` all read ``_calls``
        and are unaffected by where this is called.
        """
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

    def record_off_turn(
        self,
        tier: TierName,
        usage: Usage,
        cfg: TierConfig | None = None,
    ) -> float:
        """Record a worker (or other) call that is not a user turn.

        Distill (TD-2301) is a session-end completion: it uses worker
        pricing and the session ledger, and must not move ``turn_cost``.
        """
        record, cost = self._build_record(tier, usage, cfg)
        self._calls.append(record)
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
        cached_tokens = billable_cached_tokens(usage.cached_prompt_tokens)
        uncached = max(0, usage.prompt_tokens - cached_tokens)
        prompt_cost = round(uncached * tier_cfg.input_price / 1_000_000, 6)
        cached_cost = round(cached_tokens * tier_cfg.cache_read_price / 1_000_000, 6)
        completion_cost = round(usage.completion_tokens * tier_cfg.output_price / 1_000_000, 6)

        record = CallRecord(
            tier=tier,
            model=tier_cfg.require_slug(),
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
        """Cached prompt tokens the provider reported this turn.

        Calls that reported no figure contribute nothing, so this is a sum
        of reported reuse — never a sum that includes an assumed one.
        """
        return sum(billable_cached_tokens(c.cached_prompt_tokens) for c in self._turn_calls)

    @property
    def cache_observed(self) -> bool:
        """Whether any main-loop call has come back yet (TD-1811).

        Splits "no data" from "the provider had nothing to say about
        cache": both leave :attr:`last_cached_prompt_tokens` at ``None``,
        and only this tells the two apart.  Without it a fresh session and
        a session running on an engine that never reports reuse render
        identically, and the user cannot tell which one they are looking
        at.
        """
        return bool(self._calls)

    @property
    def last_cached_prompt_tokens(self) -> int | None:
        """Cached prompt tokens the provider reported on the last call.

        ``None`` in two cases, told apart by :attr:`cache_observed`: no
        main-loop call has landed yet, or the last one reported no cached
        figure at all.  Never ``0`` on the strength of a missing field —
        cache state is a provider-side fact and an absent one stays
        unknown (TD-1811).  Read across turns (not reset by
        ``begin_turn``): the inspector's "currently cached" signal is
        about the last observed call (TD-1201).  Classifier calls are
        excluded — they don't carry the steering block.
        """
        if not self._calls:
            return None
        return self._calls[-1].cached_prompt_tokens

    def turn_uncached_tokens(self) -> int:
        """Total uncached prompt tokens in the current turn."""
        return sum(c.uncached_prompt_tokens for c in self._turn_calls)

    def turn_cache_ratio(self) -> float:
        """Share of this turn's prompt tokens the provider reported as reused.

        ``0.0`` when the provider reported no reuse *and* when it reported
        nothing — a ratio is a claim about money saved, and there is no
        saving to claim in either case.  ``0.0`` too when the turn consumed
        no prompt tokens at all.  Turn-scoped, so a previous turn's hit
        rate can never be carried into one that had none.
        """
        total = sum(c.prompt_tokens for c in self._turn_calls)
        cached = self.turn_cached_tokens()
        return cached / total if total > 0 else 0.0

    def turn_cache_reported(self) -> bool:
        """Whether any call *this turn* carried a cache figure (TD-1814).

        Pair of :meth:`turn_cache_ratio`.  ``last_cached_prompt_tokens``
        is the last *session* call and is not reset by ``begin_turn``, so
        it cannot stand next to a turn-scoped ratio — a silent turn after
        a reporting one would otherwise log ``cache_reported: true``.
        """
        return any(c.cached_prompt_tokens is not None for c in self._turn_calls)

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
