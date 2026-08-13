"""Tests for the ambiguous-case worker classifier (TD-703).

Covers: the tight cached prompt, the (tool, argument-shape) cache,
worker-tier fallback for ambiguous cases, the fail-toward-B contract,
and separate classifier cost tracking.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest import approx

from tests.test_cost import WORKER, _usage
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    build_classifier_prompt,
    parse_decision,
)
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.cost import CostTracker

# ── Setup helpers ───────────────────────────────────────────────────────


def make_static(workspace: Path) -> DecisionClassifier:
    return DecisionClassifier(Boundary(workspace_root=workspace))


def ambiguous_request(
    tool: str = "shell", arguments: dict[str, object] | None = None
) -> DecisionRequest:
    """A request no static rule resolves (no paths/hosts declared)."""
    return DecisionRequest(
        tool_name=tool,
        arguments=arguments or {"command": "run something"},
        is_mutation=True,
    )


class SpyWorker:
    """Records calls and returns a scripted response."""

    def __init__(self, response: str = "A") -> None:
        self.response = response
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


# ── Prompt ──────────────────────────────────────────────────────────────


class TestPrompt:
    def test_instruction_prefix_is_stable(self) -> None:
        p1 = build_classifier_prompt(ambiguous_request("shell"))
        p2 = build_classifier_prompt(ambiguous_request("fs_write"))
        # The instruction block is byte-identical (provider prefix caching).
        instr1 = p1.split("Tool:")[0]
        instr2 = p2.split("Tool:")[0]
        assert instr1 == instr2
        assert "A = reversible" in instr1 and "C = irreversible" in instr1

    def test_prompt_varies_only_by_tool_call(self) -> None:
        p1 = build_classifier_prompt(ambiguous_request("shell"))
        p2 = build_classifier_prompt(ambiguous_request("shell"))
        assert p1 == p2  # deterministic

    def test_canonical_arguments_are_order_independent(self) -> None:
        r1 = DecisionRequest(tool_name="t", arguments={"a": 1, "b": 2})
        r2 = DecisionRequest(tool_name="t", arguments={"b": 2, "a": 1})
        p1 = build_classifier_prompt(r1)
        p2 = build_classifier_prompt(r2)
        assert p1 == p2


# ── Parsing ─────────────────────────────────────────────────────────────


class TestParseDecision:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("A", DecisionClass.A),
            (" B ", DecisionClass.B),
            ("C.", DecisionClass.C),
            ("c", DecisionClass.C),
            ("A", DecisionClass.A),
        ],
    )
    def test_parse_accepts_bare_letter(self, text: str, expected: DecisionClass) -> None:
        assert parse_decision(text) is expected

    @pytest.mark.parametrize("text", ["", "AB", "maybe B", "hello", "Class: A and B", "3"])
    def test_parse_rejects_anything_else(self, text: str) -> None:
        assert parse_decision(text) is None


# ── Static cases never call the worker ──────────────────────────────────


class TestStaticShortCircuit:
    async def test_static_a_skips_worker(self, tmp_path: Path) -> None:
        worker = SpyWorker()
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        request = DecisionRequest(
            tool_name="fs_edit",
            arguments={"path": str(tmp_path / "a.py")},
            writes=(tmp_path / "a.py",),
            is_mutation=True,
        )
        decision = await classifier.classify(request)
        assert decision.decision_class is DecisionClass.A
        assert worker.calls == []

    async def test_static_c_skips_worker(self, tmp_path: Path) -> None:
        worker = SpyWorker()
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        outside = tmp_path.parent / "out.txt"
        request = DecisionRequest(
            tool_name="fs_write",
            arguments={"path": str(outside)},
            writes=(outside,),
            is_mutation=True,
        )
        decision = await classifier.classify(request)
        assert decision.decision_class is DecisionClass.C
        assert worker.calls == []


# ── Ambiguous cases go to the worker ────────────────────────────────────


class TestWorkerCall:
    async def test_ambiguous_uses_worker_response(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="A")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        decision = await classifier.classify(ambiguous_request())
        assert decision.decision_class is DecisionClass.A
        assert len(worker.calls) == 1
        assert "Tool: shell" in worker.calls[0]

    async def test_worker_b_response_is_b(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="B")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        decision = await classifier.classify(ambiguous_request())
        assert decision.decision_class is DecisionClass.B


# ── Cache per (tool, argument-shape) ────────────────────────────────────


class TestCache:
    async def test_second_identical_call_is_cached(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="C")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        first = await classifier.classify(ambiguous_request())
        second = await classifier.classify(ambiguous_request())
        assert first.decision_class is DecisionClass.C
        assert second.decision_class is DecisionClass.C
        assert worker.calls == 1 * [worker.calls[0]]  # worker called exactly once

    async def test_different_shape_misses_cache(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="B")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        await classifier.classify(ambiguous_request("shell"))
        await classifier.classify(ambiguous_request("shell", arguments={"command": "other"}))
        assert len(worker.calls) == 2

    async def test_different_tool_misses_cache(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="B")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        await classifier.classify(ambiguous_request("shell"))
        await classifier.classify(ambiguous_request("git"))
        assert len(worker.calls) == 2


# ── Fail toward B, never A ──────────────────────────────────────────────


class TestFailureDefaultsToB:
    async def test_worker_exception_defaults_to_b(self, tmp_path: Path) -> None:
        async def failing(prompt: str) -> str:
            raise RuntimeError("worker down")

        classifier = AmbiguousClassifier(make_static(tmp_path), failing)
        decision = await classifier.classify(ambiguous_request())
        assert decision.decision_class is DecisionClass.B

    async def test_unparseable_worker_response_defaults_to_b(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="I am not sure, maybe do it")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        decision = await classifier.classify(ambiguous_request())
        assert decision.decision_class is DecisionClass.B

    async def test_empty_worker_response_defaults_to_b(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        decision = await classifier.classify(ambiguous_request())
        assert decision.decision_class is DecisionClass.B

    async def test_failure_is_never_class_a(self, tmp_path: Path) -> None:
        async def failing(prompt: str) -> str:
            raise RuntimeError("boom")

        classifier = AmbiguousClassifier(make_static(tmp_path), failing)
        decision = await classifier.classify(ambiguous_request())
        # Fail toward asking — never toward acting.
        assert decision.decision_class is not DecisionClass.A
        assert decision.decision_class is DecisionClass.B


# ── Classifier cost tracked separately ─────────────────────────────────


def make_config() -> ModelConfig:
    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url="http://mock.local/v1",
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=100_000,
            max_output_tokens=1_000,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("test-brain"),
                worker=tier("test-worker"),
                validator=tier("test-validator"),
            ),
        },
        active_preset="test",
    )


class TestClassifierCost:
    def test_classifier_cost_is_separate_from_main_cost(self) -> None:
        tracker = CostTracker(make_config())
        tracker.begin_turn()
        # Main-loop call on the worker tier.
        main = tracker.record("worker", _usage(1000, 0, 500), WORKER)
        # Classifier worker calls (TD-703).
        cls1 = tracker.record_classifier("worker", _usage(200, 0, 5), WORKER)
        cls2 = tracker.record_classifier("worker", _usage(200, 100, 3), WORKER)

        assert tracker.session_cost() == approx(main)
        assert tracker.turn_cost() == approx(main)
        assert tracker.classifier_cost() == approx(cls1 + cls2)
        assert tracker.classifier_call_count() == 2

    def test_summary_exposes_classifier_cost(self) -> None:
        tracker = CostTracker(make_config())
        tracker.record_classifier("worker", _usage(100, 0, 5), WORKER)
        summary = tracker.summary()
        assert summary["classifier_cost"] == approx(tracker.classifier_cost())
        assert summary["classifier_call_count"] == 1

    def test_cost_update_event_carries_classifier_cost(self) -> None:
        tracker = CostTracker(make_config())
        tracker.record_classifier("worker", _usage(100, 0, 5), WORKER)
        event = tracker.emit_cost_update(session_id="s1")
        assert event.classifier_cost == approx(tracker.classifier_cost())
        assert event.session_cost == 0.0  # main-loop cost unaffected


# ── Classification outcome shape ────────────────────────────────────────


class TestOutcome:
    async def test_cached_classification_is_explainable(self, tmp_path: Path) -> None:
        worker = SpyWorker(response="C")
        classifier = AmbiguousClassifier(make_static(tmp_path), worker)
        await classifier.classify(ambiguous_request())
        second = await classifier.classify(ambiguous_request())
        assert isinstance(second, Classification)
        assert "cached" in second.reason
        assert second.rule is None
