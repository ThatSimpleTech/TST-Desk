"""Tests for the tier routing policy (TD-303).

Covers: lead turns, validator on demand, escalation, tier emission,
and runtime override.
"""

from __future__ import annotations

import pytest

from tstd.router import TIER_NAMES, TierRouter

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def router() -> TierRouter:
    return TierRouter()


# ── Lead turns ────────────────────────────────────────────────────────────


class TestLeadTurns:
    def test_default_lead_turns(self, router: TierRouter) -> None:
        """Default lead_turns is 2."""
        assert router.lead_turns == 2

    def test_custom_lead_turns(self) -> None:
        """lead_turns is configurable."""
        r = TierRouter(lead_turns=5)
        assert r.lead_turns == 5

    def test_invalid_lead_turns(self) -> None:
        with pytest.raises(ValueError, match="lead_turns"):
            TierRouter(lead_turns=0)

    def test_brain_for_first_n_turns(self, router: TierRouter) -> None:
        """First lead_turns turns use brain."""
        assert router.record_turn_start() == "brain"  # turn 1
        assert router.record_turn_start() == "brain"  # turn 2

    def test_worker_after_lead_turns(self, router: TierRouter) -> None:
        """After lead_turns, worker takes over."""
        router.record_turn_start()  # turn 1 — brain
        router.record_turn_start()  # turn 2 — brain
        assert router.record_turn_start() == "worker"  # turn 3 — worker
        assert router.record_turn_start() == "worker"  # turn 4 — worker

    def test_lead_turn_count_tracks_turns(self, router: TierRouter) -> None:
        assert router.turn_count == 0
        router.record_turn_start()
        assert router.turn_count == 1
        router.record_turn_start()
        assert router.turn_count == 2


# ── Validator ─────────────────────────────────────────────────────────────


class TestValidator:
    def test_validator_not_scheduled(self, router: TierRouter) -> None:
        """Validator is never selected by default routing."""
        for _ in range(10):
            tier = router.record_turn_start()
            if tier == "validator":
                msg = "Validator was scheduled automatically"
                raise AssertionError(msg)

    def test_validator_on_demand(self, router: TierRouter) -> None:
        """Validator can be set via override."""
        router.set_tier("validator")
        assert router.record_turn_start() == "validator"

    def test_validator_after_worker(self, router: TierRouter) -> None:
        """Can switch to validator after normal routing has started."""
        router.record_turn_start()  # brain
        router.record_turn_start()  # brain
        router.record_turn_start()  # worker
        router.set_tier("validator")
        assert router.record_turn_start() == "validator"


# ── Escalation ────────────────────────────────────────────────────────────


class TestEscalation:
    def test_default_failure_threshold(self, router: TierRouter) -> None:
        """Default failure threshold is 3."""
        assert router.failure_threshold == 3

    def test_custom_failure_threshold(self) -> None:
        r = TierRouter(failure_threshold=2)
        assert r.failure_threshold == 2

    def test_invalid_threshold(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            TierRouter(failure_threshold=0)

    def test_escalation_after_threshold_failures(self) -> None:
        """Worker escalates to brain after consecutive failures."""
        r = TierRouter(lead_turns=1, failure_threshold=2)
        r.record_turn_start()  # brain

        # Worker starts, then fails twice
        assert r.record_turn_start() == "worker"  # turn 2
        r.record_failure()  # 1 failure
        assert r.record_turn_start() == "worker"  # turn 3, no escalation yet
        r.record_failure()  # 2 failures = threshold reached
        upgraded = r.record_failure()
        assert upgraded  # escalation triggered

        # Now the next turn should be brain
        assert r.record_turn_start() == "brain"

    def test_escalation_resets_after_brain_turn(self, router: TierRouter) -> None:
        """After escalation, brain handles one turn, then back to worker."""
        r = TierRouter(lead_turns=1, failure_threshold=1)
        r.record_turn_start()  # brain
        assert r.record_turn_start() == "worker"  # turn 2
        r.record_failure()  # 1 failure = threshold

        # Escalation: next turn is brain
        assert r.record_turn_start() == "brain"  # turn 3 — escalation
        # After the escalation turn, back to normal routing
        assert r.record_turn_start() == "worker"  # turn 4 — back to worker

    def test_success_resets_failure_counter(self, router: TierRouter) -> None:
        """A successful turn resets the consecutive failure counter."""
        r = TierRouter(lead_turns=1, failure_threshold=3)
        r.record_turn_start()  # brain
        r.record_turn_start()  # worker
        r.record_failure()  # 1
        r.record_failure()  # 2
        r.record_success()  # reset!
        r.record_failure()  # back to 1
        upgraded = r.record_failure()  # 2, not 3 — no escalation
        assert not upgraded

    def test_escalation_only_from_worker(self, router: TierRouter) -> None:
        """Failures on brain/validator do not trigger escalation."""
        router.set_tier("brain")
        router.record_turn_start()
        for _ in range(router.failure_threshold + 1):
            router.record_failure()
        # Should not escalate (already on brain)
        assert router.record_turn_start() == "brain"  # override still active

    def test_no_escalation_below_threshold(self, router: TierRouter) -> None:
        """Fewer failures than threshold does not escalate."""
        r = TierRouter(lead_turns=1, failure_threshold=5)
        r.record_turn_start()  # brain
        r.record_turn_start()  # worker
        r.record_failure()
        r.record_failure()
        r.record_failure()
        r.record_failure()
        assert r.record_turn_start() == "worker"  # still worker, 4 < 5


# ── Runtime override ──────────────────────────────────────────────────────


class TestOverride:
    def test_set_tier_takes_effect_next_turn(self, router: TierRouter) -> None:
        """set_tier takes effect on the next turn, not the current one."""
        router.set_tier("validator")
        assert router.record_turn_start() == "validator"

    def test_override_persists(self, router: TierRouter) -> None:
        """Override persists across multiple turns."""
        router.set_tier("validator")
        assert router.record_turn_start() == "validator"
        assert router.record_turn_start() == "validator"
        assert router.record_turn_start() == "validator"

    def test_clear_override(self, router: TierRouter) -> None:
        """Clearing override returns to normal routing."""
        router.set_tier("validator")
        assert router.record_turn_start() == "validator"
        router.clear_override()
        assert router.record_turn_start() == "brain"  # back to brain (turn 2)

    def test_has_override(self, router: TierRouter) -> None:
        assert not router.has_override
        router.set_tier("worker")
        assert router.has_override
        router.clear_override()
        assert not router.has_override

    def test_invalid_tier_name(self, router: TierRouter) -> None:
        with pytest.raises(ValueError, match="Invalid tier"):
            router.set_tier("invalid")  # type: ignore[arg-type]

    def test_override_any_tier(self, router: TierRouter) -> None:
        """Can override to any of the three tier names."""
        for tier in TIER_NAMES:
            r = TierRouter()
            r.set_tier(tier)
            assert r.record_turn_start() == tier


# ── Active tier emission ──────────────────────────────────────────────────


class TestActiveTier:
    def test_active_tier_property(self, router: TierRouter) -> None:
        """active_tier reports the current tier without advancing turn."""
        # Before any turn, should be brain (lead turns rule)
        assert router.active_tier == "brain"
        router.record_turn_start()
        assert router.active_tier == "brain"
        # After 2 lead turns, active_tier shows worker (next turn in line)
        # Use a fresh router to avoid mypy type narrowing
        r2 = TierRouter()
        r2.record_turn_start()
        r2.record_turn_start()
        assert r2.active_tier == "worker"

    def test_active_tier_reflects_override(self, router: TierRouter) -> None:
        router.set_tier("validator")
        assert router.active_tier == "validator"

    def test_turn_count(self, router: TierRouter) -> None:
        assert router.turn_count == 0
        router.record_turn_start()
        assert router.turn_count == 1


# ── Reset ─────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears_all_state(self, router: TierRouter) -> None:
        router.record_turn_start()  # turn 1
        router.record_turn_start()  # turn 2
        router.record_turn_start()  # turn 3
        router.record_failure()
        router.set_tier("validator")
        router.reset()
        assert router.turn_count == 0
        assert router.consecutive_failures == 0
        assert not router.has_override
        assert router.active_tier == "brain"

    def test_reset_allows_new_session(self, router: TierRouter) -> None:
        """After reset, routing starts fresh with brain."""
        router.record_turn_start()  # turn 1 — brain
        router.record_turn_start()  # turn 2 — brain
        router.record_turn_start()  # turn 3 — worker
        router.reset()
        assert router.record_turn_start() == "brain"  # fresh start


# ── Summary ────────────────────────────────────────────────────────────────


class TestSummary:
    def test_summary_keys(self, router: TierRouter) -> None:
        s = router.summary()
        assert "active_tier" in s
        assert "turn_count" in s
        assert "consecutive_failures" in s
        assert "lead_turns" in s
        assert "failure_threshold" in s
        assert "override" in s

    def test_summary_after_turns(self, router: TierRouter) -> None:
        router.record_turn_start()
        router.record_turn_start()
        router.record_failure()
        s = router.summary()
        assert s["turn_count"] == 2
        # After 2 turns, active_tier is worker (lead_turns=2, 2 < 2 is False)
        assert s["active_tier"] == "worker"
        assert s["consecutive_failures"] == 1


# ── Plan lock (TD-4603) ───────────────────────────────────────────────────


class TestPlanLock:
    def test_lock_off_by_default(self, router: TierRouter) -> None:
        assert router.plan_lock is False

    def test_lock_forces_brain_past_lead_turns(self, router: TierRouter) -> None:
        """With the lock on, brain serves turns that would route to worker."""
        router.record_turn_start()
        router.record_turn_start()  # lead turns exhausted; next would be worker
        router.set_plan_lock(True)
        assert router.active_tier == "brain"
        assert router.record_turn_start() == "brain"

    def test_lock_beats_override(self, router: TierRouter) -> None:
        """The lock wins even over a pinned worker override."""
        router.set_tier("worker")
        router.set_plan_lock(True)
        assert router.active_tier == "brain"

    def test_set_tier_to_worker_refused_while_locked(self, router: TierRouter) -> None:
        router.set_plan_lock(True)
        with pytest.raises(ValueError, match="plan lock"):
            router.set_tier("worker")

    def test_set_tier_to_validator_refused_while_locked(self, router: TierRouter) -> None:
        router.set_plan_lock(True)
        with pytest.raises(ValueError, match="plan lock"):
            router.set_tier("validator")

    def test_set_tier_to_brain_allowed_while_locked(self, router: TierRouter) -> None:
        router.set_plan_lock(True)
        router.set_tier("brain")
        assert router.override == "brain"
        assert router.active_tier == "brain"

    def test_clearing_restores_prior_routing(self, router: TierRouter) -> None:
        """A pinned override held underneath the lock resumes on clear."""
        router.set_tier("worker")
        router.set_plan_lock(True)
        assert router.active_tier == "brain"
        router.set_plan_lock(False)
        assert router.plan_lock is False
        assert router.active_tier == "worker"

    def test_reset_clears_lock(self, router: TierRouter) -> None:
        router.set_plan_lock(True)
        router.reset()
        assert router.plan_lock is False

    def test_summary_reports_lock(self, router: TierRouter) -> None:
        router.set_plan_lock(True)
        assert router.summary()["plan_lock"] is True
