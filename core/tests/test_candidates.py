"""Candidate selection tests (TD-711, dev build).

Code extracts candidates; the judgment backend picks; code acts.  Every
non-assertion — no candidates, failed or low-confidence judgment, an
explicit 'none', an out-of-set answer — returns None, and the caller
falls back to today's brain-driven path.
"""

from __future__ import annotations

from tstd.autonomy import Judgment, ScriptedJudgmentBackend, ideal_judgment
from tstd.browser import (
    Candidate,
    candidate_from_node,
    normalize_hit,
    render_candidates,
    scripted_hit_node,
    select_candidate,
)


def _node(role: str = "button", name: str = "Sign in", **attrs: str) -> dict[str, object]:
    return {
        "xpath": "//button[1]",
        "role": role,
        "attributes": {"AXTitle": name, **attrs},
        "box": {"x": 10.0, "y": 20.0, "width": 80.0, "height": 24.0},
        "styles": {},
    }


def _candidates() -> list[Candidate]:
    return [
        Candidate(index=0, tag="button", role="button", name="Cancel", box=(1, 2, 3, 4)),
        Candidate(index=1, tag="button", role="button", name="Sign in", box=(5, 6, 7, 8)),
        Candidate(index=2, tag="a", role="link", name="Forgot password", box=(9, 10, 11, 12)),
    ]


class TestCandidateFromNode:
    def test_full_node(self) -> None:
        candidate = candidate_from_node(_node(), 1)
        assert candidate is not None
        assert candidate.index == 1
        assert candidate.role == "button"
        assert candidate.name == "Sign in"
        assert candidate.box == (10.0, 20.0, 80.0, 24.0)

    def test_name_preference_order(self) -> None:
        node = _node(name="")
        node["attributes"] = {"id": "fallback-id", "aria-label": "Close dialog"}
        candidate = candidate_from_node(node, 0)
        assert candidate is not None
        assert candidate.name == "Close dialog"

    def test_role_and_name_both_missing_is_none(self) -> None:
        assert candidate_from_node({"box": {}}, 0) is None

    def test_normalize_hit_shape_roundtrips(self) -> None:
        node = normalize_hit(scripted_hit_node(50, 60), 50, 60)
        candidate = candidate_from_node(node, 0)
        assert candidate is not None
        assert candidate.role == "button"
        assert candidate.name == "mock-target"


class TestRenderCandidates:
    def test_one_line_per_candidate(self) -> None:
        text = render_candidates(_candidates(), 2000)
        assert "0: [button] 'Cancel'" in text
        assert "1: [button] 'Sign in'" in text
        assert "2: [link] 'Forgot password'" in text

    def test_capped(self) -> None:
        text = render_candidates(_candidates(), 30)
        assert len(text) <= 30
        assert text.endswith("…")


class TestSelectCandidate:
    async def test_picks_the_judged_index(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("1")])
        picked = await select_candidate(backend, "the sign-in button", _candidates())
        assert picked == 1
        # The payload is the isolation schema: target + rendered candidates.
        state_keys = [key for key, _ in backend.questions[0].state]
        assert state_keys == ["Target", "Candidates"]

    async def test_explicit_none_is_no_pick(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("none")])
        assert await select_candidate(backend, "missing", _candidates()) is None

    async def test_low_confidence_is_no_pick(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("1", 0.4)])
        assert await select_candidate(backend, "sign in", _candidates(), threshold=0.6) is None

    async def test_out_of_set_answer_is_no_pick(self) -> None:
        backend = ScriptedJudgmentBackend(
            [Judgment(label="7", confidence=1.0, backend="x", latency_ms=0.0)]
        )
        assert await select_candidate(backend, "sign in", _candidates()) is None

    async def test_failed_judgment_is_no_pick(self) -> None:
        backend = ScriptedJudgmentBackend()  # empty → failed judgment
        assert await select_candidate(backend, "sign in", _candidates()) is None

    async def test_no_candidates_never_calls_backend(self) -> None:
        backend = ScriptedJudgmentBackend([ideal_judgment("0")])
        assert await select_candidate(backend, "sign in", []) is None
        assert backend.questions == []
