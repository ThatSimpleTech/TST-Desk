"""Judgment seam tests (TD-708, dev build).

The seam is the infrastructure; connectors are pluggable.  These tests
pin the fail-closed contract every connector must keep and prove the
classifier's default path through the seam is byte-identical to the
pre-seam worker call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    Judgment,
    JudgmentKind,
    JudgmentQuestion,
    ScriptedJudgmentBackend,
    WorkerChatJudgmentBackend,
    build_classifier_prompt,
    ideal_judgment,
    parse_judgment,
    render_question_prompt,
)


def _question(**overrides: object) -> JudgmentQuestion:
    base: dict[str, object] = {
        "kind": JudgmentKind.CHOICE,
        "instructions": "Pick one.",
        "state": (("Tool", "custom_tool"),),
        "options": ("A", "B", "C"),
    }
    base.update(overrides)
    return JudgmentQuestion(**base)  # type: ignore[arg-type]


def _ambiguous(q: int = 1) -> DecisionRequest:
    """A request no static rule fires on — the ambiguous case."""
    return DecisionRequest(tool_name="custom_tool", arguments={"q": q})


def _static(tmp_path: Path) -> DecisionClassifier:
    return DecisionClassifier(Boundary(workspace_root=tmp_path))


class TestPromptRendering:
    def test_generic_renderer_shape(self) -> None:
        prompt = render_question_prompt(_question())
        assert prompt.startswith("Pick one.\n\n")
        assert "Tool: custom_tool" in prompt
        assert "Answer with exactly one of: A, B, C." in prompt
        assert prompt.endswith("Answer:")

    def test_noul_defaults_to_no_yes(self) -> None:
        question = JudgmentQuestion(kind=JudgmentKind.NOUL, instructions="Did it work?")
        assert question.resolved_options() == ("no", "yes")
        assert "no, yes" in render_question_prompt(question)


class TestParseJudgment:
    def test_exact_and_case_insensitive(self) -> None:
        assert parse_judgment("A", ("A", "B", "C")) == "A"
        assert parse_judgment(" b ", ("A", "B", "C")) == "B"
        assert parse_judgment("yes", ("no", "yes")) == "yes"

    def test_punctuation_stripped(self) -> None:
        assert parse_judgment("B.", ("A", "B", "C")) == "B"

    def test_prose_and_empty_fail_closed(self) -> None:
        assert parse_judgment("I choose A", ("A", "B", "C")) is None
        assert parse_judgment("", ("A", "B", "C")) is None
        assert parse_judgment("AB", ("A", "B", "C")) is None


class TestWorkerChatConnector:
    async def test_clean_reply_is_ok_with_full_confidence(self) -> None:
        async def complete(prompt: str) -> str:
            return "B"

        backend = WorkerChatJudgmentBackend(complete)
        judgment = await backend.judge(_question())
        assert judgment.ok
        assert judgment.label == "B"
        assert judgment.confidence == 1.0
        assert judgment.backend == "worker"

    async def test_error_fails_closed_without_raising(self) -> None:
        async def boom(prompt: str) -> str:
            raise RuntimeError("provider down")

        judgment = await WorkerChatJudgmentBackend(boom).judge(_question())
        assert not judgment.ok
        assert judgment.reason == "error"

    async def test_unparseable_fails_closed(self) -> None:
        async def junk(prompt: str) -> str:
            return "let me think about this"

        judgment = await WorkerChatJudgmentBackend(junk).judge(_question())
        assert not judgment.ok
        assert judgment.reason == "parse_failure"


class TestScriptedBackend:
    async def test_queue_order_and_recording(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("yes"), ideal_judgment("no", 0.7)])
        first = await backend.judge(_question())
        second = await backend.judge(_question())
        assert (first.label, second.label) == ("yes", "no")
        assert second.confidence == 0.7
        assert len(backend.questions) == 2

    async def test_empty_queue_fails_closed(self) -> None:
        judgment = await ScriptedJudgmentBackend().judge(_question())
        assert not judgment.ok
        assert judgment.reason == "empty_script"


class TestClassifierThroughSeam:
    async def test_backend_labels_map_to_decision_classes(self, tmp_path: Path) -> None:
        pairs = (("A", DecisionClass.A), ("B", DecisionClass.B), ("C", DecisionClass.C))
        for label, expected in pairs:
            classifier = AmbiguousClassifier(
                _static(tmp_path), backend=ScriptedJudgmentBackend([ideal_judgment(label)])
            )
            decision = await classifier.classify(_ambiguous())
            assert decision.decision_class is expected

    async def test_low_confidence_defaults_to_b(self, tmp_path: Path) -> None:
        classifier = AmbiguousClassifier(
            _static(tmp_path),
            backend=ScriptedJudgmentBackend([ideal_judgment("A", 0.3)]),
            min_confidence=0.6,
        )
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.B

    async def test_failed_judgment_defaults_to_b(self, tmp_path: Path) -> None:
        classifier = AmbiguousClassifier(_static(tmp_path), backend=ScriptedJudgmentBackend())
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.B

    async def test_out_of_set_label_defaults_to_b(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend(
            [Judgment(label="D", confidence=1.0, backend="x", latency_ms=0.0)]
        )
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend)
        decision = await classifier.classify(_ambiguous())
        assert decision.decision_class is DecisionClass.B

    async def test_static_short_circuit_never_calls_backend(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("A")])
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend)
        outside = DecisionRequest(tool_name="fs_read", reads=(Path("/etc/passwd"),))
        decision = await classifier.classify(outside)
        assert decision.decision_class is DecisionClass.C
        assert backend.questions == []

    async def test_cache_avoids_repeat_judgment(self, tmp_path: Path) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("B")])
        classifier = AmbiguousClassifier(_static(tmp_path), backend=backend)
        await classifier.classify(_ambiguous())
        second = await classifier.classify(_ambiguous())
        assert len(backend.questions) == 1
        assert "cached" in second.reason

    async def test_neither_backend_nor_call_worker_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="backend or call_worker"):
            AmbiguousClassifier(_static(tmp_path))

    async def test_default_connector_prompt_is_legacy_bytes(self, tmp_path: Path) -> None:
        """call_worker wrapping keeps the TD-703 prompt byte-for-byte."""
        seen: list[str] = []

        async def spy(prompt: str) -> str:
            seen.append(prompt)
            return "B"

        classifier = AmbiguousClassifier(_static(tmp_path), call_worker=spy)
        request = _ambiguous()
        await classifier.classify(request)
        assert seen == [build_classifier_prompt(request)]
        assert seen[0].endswith("Class:")
