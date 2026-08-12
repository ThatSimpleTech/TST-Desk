"""Tier routing policy — determines which model handles each turn.

Routing rules (TD-303):
- Brain tier handles the first ``lead_turns`` (default 2), then worker takes over.
- Validator is invoked on demand only (never scheduled in v0.1).
- Worker may escalate back to brain after ``failure_threshold`` consecutive failures.
- Runtime override via ``set_tier`` persists until changed or cleared.
"""

from __future__ import annotations

from typing import Literal

TierName = Literal["brain", "worker", "validator"]
TIER_NAMES: tuple[TierName, ...] = ("brain", "worker", "validator")

LEAD_TURNS_DEFAULT = 2
FAILURE_THRESHOLD_DEFAULT = 3


class TierRouter:
    """Determines the active model tier for each turn.

    Usage::

        router = TierRouter()
        router.record_turn_start()   # called at the beginning of a turn
        tier = router.active_tier    # "brain" for turns 1-2, "worker" after
        ...
        # After a tool call result:
        router.record_success()      # clears consecutive failure counter
        router.record_failure()      # increments counter; may trigger escalation
    """

    def __init__(
        self,
        lead_turns: int = LEAD_TURNS_DEFAULT,
        failure_threshold: int = FAILURE_THRESHOLD_DEFAULT,
    ) -> None:
        if lead_turns < 1:
            raise ValueError(f"lead_turns must be >= 1, got {lead_turns}")
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")

        self._lead_turns = lead_turns
        self._failure_threshold = failure_threshold

        # Internal state
        self._turn_count = 0
        self._consecutive_failures = 0
        self._override: TierName | None = None
        self._last_tier: TierName = "brain"
        self._escalated: bool = False

    # ── Public API ─────────────────────────────────────────────────────

    @property
    def active_tier(self) -> TierName:
        """The tier that should handle the current (or next) turn.

        Priority:
        1. Explicit override (set_tier)
        2. Escalation (worker -> brain after repeated failures)
        3. Lead turns (brain for first N turns)
        4. Default fallback (worker)
        """
        if self._override is not None:
            return self._override

        # Check if we need escalation from worker -> brain
        if self._escalated:
            return "brain"

        # Lead turns use brain
        if self._turn_count < self._lead_turns:
            return "brain"

        # Default to worker
        return "worker"

    @property
    def turn_count(self) -> int:
        """Number of completed turns."""
        return self._turn_count

    @property
    def consecutive_failures(self) -> int:
        """Consecutive failures since last success."""
        return self._consecutive_failures

    @property
    def lead_turns(self) -> int:
        return self._lead_turns

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    def record_turn_start(self) -> TierName:
        """Record the start of a new turn.

        Returns the active tier for this turn.

        Must be called before ``record_success`` or ``record_failure`` for
        the turn.
        """
        tier = self.active_tier
        self._last_tier = tier
        self._turn_count += 1

        # If we just served an escalation, reset escalation flag so
        # the next turn goes back to the normal routing.
        if self._escalated:
            self._escalated = False
            self._consecutive_failures = 0

        return tier

    def record_success(self) -> None:
        """Record a successful turn.

        Resets the consecutive failure counter.
        """
        self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """Record a failed turn.

        Returns:
            True if the failure triggered an escalation (worker -> brain),
            False otherwise.
        """
        self._consecutive_failures += 1

        # Escalate if worker is active and failures exceed threshold
        if self._last_tier == "worker" and self._consecutive_failures >= self._failure_threshold:
            self._escalated = True
            return True

        return False

    def set_tier(self, tier: TierName) -> None:
        """Override the active tier. Takes effect on the next turn."""
        if tier not in TIER_NAMES:
            raise ValueError(f"Invalid tier: {tier!r}. Must be one of {TIER_NAMES}")
        self._override = tier

    def clear_override(self) -> None:
        """Remove a runtime override, returning to normal routing."""
        self._override = None

    @property
    def has_override(self) -> bool:
        """Whether a runtime override is active."""
        return self._override is not None

    def reset(self) -> None:
        """Reset the router to its initial state (for a new session)."""
        self._turn_count = 0
        self._consecutive_failures = 0
        self._override = None
        self._last_tier = "brain"
        self._escalated = False

    # ── Serialization / inspection ────────────────────────────────────

    def summary(self) -> dict[str, int | str | bool | None]:
        """Return a snapshot of the router's state for debugging."""
        return {
            "active_tier": self.active_tier,
            "turn_count": self._turn_count,
            "consecutive_failures": self._consecutive_failures,
            "lead_turns": self._lead_turns,
            "failure_threshold": self._failure_threshold,
            "override": self._override,
            "escalated": self._escalated,
        }
