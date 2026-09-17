"""Semantic no-progress breaker tests (TD-710, dev build).

The fifth breaker judges whether the run is making measurable progress
toward the charter objective.  It is inert without a backend, on a
failed or low-confidence judgment, and for interactive sessions — the
fallback is exactly today's syntactic-only behavior.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_checkpoint import make_repo
from tstd.autonomy import ScriptedJudgmentBackend, ideal_judgment
from tstd.autonomy.breakers import SEMANTIC_NO_PROGRESS, maybe_trip_semantic
from tstd.autonomy.runner import advance_autonomy
from tstd.session import Session


def _no(confidence: float = 0.95) -> object:
    return ideal_judgment("no", confidence)


def _yes(confidence: float = 0.95) -> object:
    return ideal_judgment("yes", confidence)


def _autonomy_session(workspace: Path) -> Session:
    session = Session(str(workspace))
    session.autonomy = True
    session.charter = make_charter()
    return session


class TestMaybeTripSemantic:
    async def test_trips_after_limit_consecutive_no(self, tmp_path: Path) -> None:
        session = _autonomy_session(tmp_path)
        backend = ScriptedJudgmentBackend([_no(), _no(), _no()])  # type: ignore[list-item]
        assert await maybe_trip_semantic(session, backend) is None
        assert await maybe_trip_semantic(session, backend) is None
        assert await maybe_trip_semantic(session, backend) == SEMANTIC_NO_PROGRESS
        assert session.semantic_no_progress_streak == 3

    async def test_progress_resets_the_streak(self, tmp_path: Path) -> None:
        session = _autonomy_session(tmp_path)
        backend = ScriptedJudgmentBackend([_no(), _yes(), _no(), _no()])  # type: ignore[list-item]
        for _ in range(4):
            assert await maybe_trip_semantic(session, backend) is None
        assert session.semantic_no_progress_streak == 2

    async def test_inert_without_backend(self, tmp_path: Path) -> None:
        session = _autonomy_session(tmp_path)
        assert await maybe_trip_semantic(session, None) is None
        assert session.semantic_no_progress_streak == 0

    async def test_inert_for_interactive_sessions(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))  # autonomy off
        backend = ScriptedJudgmentBackend([_no()])  # type: ignore[list-item]
        assert await maybe_trip_semantic(session, backend) is None
        assert backend.questions == []

    async def test_inert_on_failed_judgment(self, tmp_path: Path) -> None:
        session = _autonomy_session(tmp_path)
        backend = ScriptedJudgmentBackend()  # empty script → failed judgment
        for _ in range(5):
            assert await maybe_trip_semantic(session, backend) is None
        assert session.semantic_no_progress_streak == 0

    async def test_inert_on_low_confidence(self, tmp_path: Path) -> None:
        session = _autonomy_session(tmp_path)
        backend = ScriptedJudgmentBackend([_no(0.3), _no(0.3), _no(0.3)])  # type: ignore[list-item]
        for _ in range(3):
            assert await maybe_trip_semantic(session, backend, threshold=0.6) is None
        assert session.semantic_no_progress_streak == 0

    async def test_raising_backend_never_trips(self, tmp_path: Path) -> None:
        class _Boom:
            @property
            def name(self) -> str:
                return "boom"

            async def judge(self, _question: object) -> object:
                raise RuntimeError("down")

        session = _autonomy_session(tmp_path)
        assert await maybe_trip_semantic(session, _Boom()) is None  # type: ignore[arg-type]


class TestRunnerIntegration:
    async def test_runner_stops_on_semantic_no_progress(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        _wire_autonomy(session, make_charter(max_iterations=8))
        session.judgment_backend = ScriptedJudgmentBackend(  # type: ignore[arg-type]
            [_no(), _no(), _no()]
        )
        # Two turns continue; the third consecutive no-progress trips.
        assert await advance_autonomy(session) is True
        assert await advance_autonomy(session) is True
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == SEMANTIC_NO_PROGRESS

    async def test_runner_without_backend_never_judges(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        _wire_autonomy(session, make_charter(max_iterations=1))
        # No judgment_backend: the run ends on the iteration cap, and the
        # semantic breaker never fires.
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason is not None
        assert "iteration cap" in session.autonomy_stop_reason
        assert session.semantic_no_progress_streak == 0
