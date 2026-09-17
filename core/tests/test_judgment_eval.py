"""Judgment seam eval (TD-708/709/710 dev build): judged paths vs baselines.

Run with ``pytest -s`` to see the comparison tables.  Mock backends only —
no live model, no network, no spend.  The ``ideal`` backend stands in for
a perfect typed model: it is the ceiling the seam allows, not a claim
about any vendor.  Live accuracy/latency numbers are the hardening path
(recorded fixtures against real connectors, per the stories).
"""

from __future__ import annotations

from pathlib import Path

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    ScriptedJudgmentBackend,
    ideal_judgment,
)
from tstd.autonomy.breakers import maybe_trip, maybe_trip_semantic, record_tool_round
from tstd.cu_verify import ActuationVerifier
from tstd.session import Session

# ── Classifier fixtures: ambiguous to the static table, known classes ──

_CLASSIFIER_FIXTURES: list[tuple[DecisionRequest, DecisionClass]] = [
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 1}), DecisionClass.A),
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 2}), DecisionClass.C),
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 3}), DecisionClass.B),
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 4}), DecisionClass.A),
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 5}), DecisionClass.C),
    (DecisionRequest(tool_name="custom_tool", arguments={"q": 6}), DecisionClass.B),
]

# ── Verification fixtures: (before, after, output, landed?) ───────────

_VERIFICATION_FIXTURES: list[tuple[str, str, str, bool]] = [
    ("url=about:blank title=Blank", "url=app.local title=Dashboard", "navigated", True),
    ("app=code title=Editor", "app=code title=Save dialog", "clicked", True),
    ("url=app.local title=Login", "url=app.local title=Login", "clicked", False),
    ("app=code title=Editor", "app=code title=Editor", "typed", False),
    ("url=app.local title=Cart", "url=app.local title=Checkout", "clicked", True),
    ("url=app.local title=Cart", "url=app.local title=Cart", "clicked", False),
]


class TestClassifierEval:
    async def test_judged_paths_beat_static_only_baseline(self, tmp_path: Path) -> None:
        boundary = Boundary(workspace_root=tmp_path)

        # Baseline: the pre-TD-703 behavior — static table only, and any
        # ambiguous case defaults to B without a model call.
        baseline_correct = 0
        for request, expected in _CLASSIFIER_FIXTURES:
            decision = DecisionClassifier(boundary).classify(request)
            assert decision.decision_class is None  # fixtures are genuinely ambiguous
            if DecisionClass.B is expected:
                baseline_correct += 1

        # Default connector: worker chat completion with scripted correct
        # replies — the real prompt-render + strict-parse path.
        answers = [expected.value for _request, expected in _CLASSIFIER_FIXTURES]

        async def scripted_worker(prompt: str) -> str:
            return answers.pop(0)

        worker = AmbiguousClassifier(DecisionClassifier(boundary), call_worker=scripted_worker)
        worker_correct = 0
        for request, expected in _CLASSIFIER_FIXTURES:
            decision = await worker.classify(request)
            if decision.decision_class is expected:
                worker_correct += 1

        # Ideal typed backend: the ceiling — typed labels, no parse step.
        ideal = AmbiguousClassifier(
            DecisionClassifier(boundary),
            backend=ScriptedJudgmentBackend(
                [ideal_judgment(expected.value) for _r, expected in _CLASSIFIER_FIXTURES]
            ),
        )
        ideal_correct = 0
        for request, expected in _CLASSIFIER_FIXTURES:
            decision = await ideal.classify(request)
            if decision.decision_class is expected:
                ideal_correct += 1

        total = len(_CLASSIFIER_FIXTURES)
        print("\nClassifier eval (ambiguous cases):")
        print("| path | correct | accuracy | model calls |")
        print("| --- | --- | --- | --- |")
        print(
            f"| static-only baseline (→ B) | {baseline_correct}/{total} | "
            f"{baseline_correct / total:.0%} | 0 |"
        )
        print(
            f"| worker connector (default) | {worker_correct}/{total} | "
            f"{worker_correct / total:.0%} | {total} |"
        )
        print(
            f"| ideal typed backend (ceiling) | {ideal_correct}/{total} | "
            f"{ideal_correct / total:.0%} | {total} |"
        )

        assert worker_correct == total
        assert ideal_correct == total
        assert baseline_correct < total

    async def test_cache_avoids_repeat_spend(self, tmp_path: Path) -> None:
        calls = 0

        async def counting_worker(prompt: str) -> str:
            nonlocal calls
            calls += 1
            return "B"

        classifier = AmbiguousClassifier(
            DecisionClassifier(Boundary(workspace_root=tmp_path)),
            call_worker=counting_worker,
        )
        request = DecisionRequest(tool_name="custom_tool", arguments={"q": 1})
        for _ in range(5):
            await classifier.classify(request)
        assert calls == 1


class TestVerificationEval:
    async def test_verifier_catches_bad_actuations_baseline_cannot(self) -> None:
        # Baseline: no verifier — a refuted actuation is invisible.
        baseline_caught = 0

        backend = ScriptedJudgmentBackend(
            [
                ideal_judgment("yes" if landed else "no")
                for _b, _a, _o, landed in _VERIFICATION_FIXTURES
            ]
        )
        verifier = ActuationVerifier(backend)
        judged_caught = 0
        false_refutations = 0
        for before, after, output, landed in _VERIFICATION_FIXTURES:
            outcome = await verifier.verify("browser_click", {}, before, after, output)
            if not landed and outcome.status == "refuted":
                judged_caught += 1
            if landed and outcome.status == "refuted":
                false_refutations += 1

        bad = sum(1 for _b, _a, _o, landed in _VERIFICATION_FIXTURES if not landed)
        good = len(_VERIFICATION_FIXTURES) - bad
        print("\nVerification eval (actuation outcomes):")
        print("| path | bad outcomes caught | false refutations |")
        print("| --- | --- | --- |")
        print(f"| no verification (today) | {baseline_caught}/{bad} | 0/{good} |")
        print(f"| verifier + ideal backend | {judged_caught}/{bad} | {false_refutations}/{good} |")

        assert judged_caught == bad
        assert false_refutations == 0
        assert baseline_caught == 0


class TestBreakerEval:
    async def test_semantic_breaker_stops_spinning_run_syntactic_cannot(
        self, tmp_path: Path
    ) -> None:
        # A spinning run that keeps *changing* its tool calls: the
        # syntactic tool-loop breaker never fires on it.
        syntactic = Session(str(tmp_path))
        syntactic.autonomy = True
        for round_n in range(10):
            record_tool_round(syntactic, [("browser_click", {"x": round_n, "y": round_n})])
            assert maybe_trip(syntactic) is None

        # The semantic breaker with a backend that keeps judging
        # "no progress" trips at the limit.
        semantic = Session(str(tmp_path))
        semantic.autonomy = True
        from tests.test_autonomy_loop import make_charter

        semantic.charter = make_charter()
        backend = ScriptedJudgmentBackend([ideal_judgment("no")] * 10)
        tripped_at: int | None = None
        for round_n in range(1, 11):
            record_tool_round(semantic, [("browser_click", {"x": round_n, "y": round_n})])
            reason = await maybe_trip_semantic(semantic, backend)
            if reason is not None:
                tripped_at = round_n
                break

        print("\nBreaker eval (spinning run, 10 rounds):")
        print("| path | stopped? | at round |")
        print("| --- | --- | --- |")
        print("| syntactic breakers only (today) | no | — |")
        print(f"| + semantic breaker | {'yes' if tripped_at else 'no'} | {tripped_at} |")

        assert tripped_at == 3

    async def test_semantic_breaker_leaves_progressing_runs_alone(self, tmp_path: Path) -> None:
        from tests.test_autonomy_loop import make_charter

        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter()
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")] * 10)
        for round_n in range(10):
            record_tool_round(session, [("fs_write", {"path": f"file_{round_n}.py"})])
            assert await maybe_trip_semantic(session, backend) is None
        assert session.semantic_no_progress_streak == 0


class TestCandidateSelectionEval:
    """TD-711: the schema payload vs the raw node dump, and pick accuracy."""

    async def test_schema_payload_is_much_smaller_and_picks_right(self) -> None:
        import json as _json

        from tstd.browser import MockBrowserDriver, candidate_from_node, render_candidates
        from tstd.browser.candidates import select_candidate

        driver = MockBrowserDriver()
        nodes = await driver.extract_candidates()
        candidates = [
            c
            for i, n in enumerate(nodes)
            if (c := candidate_from_node(n, i)) is not None
        ]

        # Baseline: the brain reads the raw node dump (what a DOM snapshot
        # costs the context window). Judgment path: the schema render.
        raw_dump = _json.dumps(nodes)
        schema_payload = render_candidates(candidates, 2000)

        backend = ScriptedJudgmentBackend([ideal_judgment("1")])
        picked = await select_candidate(backend, "the sign-in button", candidates)

        print("\nCandidate selection eval:")
        print("| path | payload chars | model calls | picked |")
        print("| --- | --- | --- | --- |")
        print(f"| raw node dump to the brain | {len(raw_dump)} | 1 brain turn | (reasoned) |")
        print(
            f"| schema + judgment | {len(schema_payload)} | 1 judgment | "
            f"{candidates[picked].name if picked is not None else None!r} |"
        )

        assert picked == 1
        assert candidates[picked].name == "Sign in"
        assert len(schema_payload) < len(raw_dump)
