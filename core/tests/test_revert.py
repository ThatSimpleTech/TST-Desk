"""Drift reverts (TD-4202) — last good checkpoint, streak, no verify revert."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_autonomy_loop import make_charter
from tests.test_checkpoint import _git, make_repo
from tests.test_dispatch import make_config
from tstd.autonomy import Checkpointer, auto_branch
from tstd.autonomy.checkpoint import auto_ref
from tstd.autonomy.classifier import DecisionClass
from tstd.autonomy.revert import (
    DRIFT_STOP,
    apply_drift_result,
    restore_auto_branch,
)
from tstd.autonomy.runner import CONTINUE_PREFIX, advance_autonomy, should_notify
from tstd.autonomy.verify import maybe_verify_after_turn, note_tool_result
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.protocol import VerifyResult
from tstd.router import TierRouter
from tstd.session import Session

CLEAN_JSON = '{"serves_objective": true, "class_a_drifted": false, "progress_real": true}'
DRIFT_JSON = '{"serves_objective": false, "class_a_drifted": true, "progress_real": false}'


def _queued(session: Session) -> int:
    return session._user_message_queue.qsize()


def _worker_router() -> TierRouter:
    router = TierRouter(lead_turns=2)
    router.record_turn_start()
    router.record_turn_start()
    router.record_turn_start()
    assert router.active_tier == "worker"
    return router


async def _checkpoint(repo: Path, branch: str, paths: list[Path], call_id: str = "tc-1") -> str:
    cp = Checkpointer(repo, "sess-1", branch=branch)
    outcome = await cp.checkpoint(
        paths,
        tool_name="fs_write",
        tool_call_id=call_id,
        decision_class=DecisionClass.A,
        decision_rule="workspace-write",
    )
    assert outcome.status == "committed"
    assert outcome.commit is not None
    return outcome.commit


def _session(repo: Path, *, max_iterations: int = 20) -> Session:
    session = Session(str(repo))
    session.autonomy = True
    session.charter = make_charter(max_iterations=max_iterations)
    session.autonomy_check_every = 1
    session.router = _worker_router()
    return session


async def _seed_good(repo: Path) -> tuple[str, str]:
    charter = make_charter()
    branch = auto_branch(charter.slug)
    (repo / "feature.txt").write_text("good\n", encoding="utf-8")
    good = await _checkpoint(repo, branch, [repo / "feature.txt"], "good")
    return branch, good


async def _write_drift(repo: Path, branch: str) -> str:
    (repo / "feature.txt").write_text("drifted\n", encoding="utf-8")
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")
    return await _checkpoint(repo, branch, [repo / "feature.txt", repo / "extra.txt"], "drift")


def _bind_validator(session: Session, raw: str) -> None:
    async def _call(_prompt: str) -> str:
        return raw

    session.validator_call = _call


# ── Single drift reverts and continues ───────────────────────────────────


async def test_single_drift_reverts_to_last_good_and_continues(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    head_before = _git(repo, "rev-parse", "HEAD")
    branch, _seed = await _seed_good(repo)
    session = _session(repo)
    _bind_validator(session, CLEAN_JSON)

    assert await advance_autonomy(session) is True
    good = session.autonomy_last_good_sha
    assert good is not None
    assert session.autonomy_drift_streak == 0
    assert _queued(session) == 1

    await _write_drift(repo, branch)
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "drifted\n"
    _bind_validator(session, DRIFT_JSON)

    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 1
    assert session.autonomy_stop_reason is None
    assert _queued(session) == 2
    msg = session._user_message_queue.get_nowait()
    session._user_message_queue.get_nowait()
    assert CONTINUE_PREFIX in msg
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "good\n"
    assert not (repo / "extra.txt").exists()
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == good
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert session.router is not None
    assert session.router.override == "brain"
    assert session.router.active_tier == "brain"


# ── Twice in a row stops ─────────────────────────────────────────────────


async def test_drift_twice_in_a_row_stops_and_notifies(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    branch, _seed = await _seed_good(repo)
    session = _session(repo)
    notified: list[str] = []

    async def _notify(message: str) -> None:
        notified.append(message)

    session.autonomy_notify = _notify
    _bind_validator(session, CLEAN_JSON)
    assert await advance_autonomy(session) is True
    await _write_drift(repo, branch)
    _bind_validator(session, DRIFT_JSON)

    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 1
    assert _queued(session) == 2

    assert await advance_autonomy(session) is False
    assert session.autonomy_stop_reason == DRIFT_STOP
    assert should_notify(session.autonomy_stop_reason)
    assert _queued(session) == 2
    assert notified
    assert any(DRIFT_STOP in item or "breaker:" in item for item in notified)


# ── Clean check resets the streak ────────────────────────────────────────


async def test_clean_check_between_drifts_resets_streak(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    branch, _seed = await _seed_good(repo)
    session = _session(repo)
    answers = iter([CLEAN_JSON, DRIFT_JSON, CLEAN_JSON, DRIFT_JSON])

    async def _call(_prompt: str) -> str:
        return next(answers)

    session.validator_call = _call
    assert await advance_autonomy(session) is True
    await _write_drift(repo, branch)
    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 1
    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 0
    await _write_drift(repo, branch)
    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 1
    assert session.autonomy_stop_reason is None
    assert _queued(session) == 4


# ── First drift with no last good ────────────────────────────────────────


async def test_first_drift_without_last_good_is_noop_and_counts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    branch, good = await _seed_good(repo)
    await _write_drift(repo, branch)
    session = _session(repo)
    _bind_validator(session, DRIFT_JSON)

    assert session.autonomy_last_good_sha is None
    assert await advance_autonomy(session) is True
    assert session.autonomy_drift_streak == 1
    assert session.autonomy_stop_reason is None
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "drifted\n"
    assert (repo / "extra.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") != good
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before
    assert _queued(session) == 1


# ── Never writes main ────────────────────────────────────────────────────


async def test_restore_never_writes_refs_heads_main(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    branch, good = await _seed_good(repo)
    await _write_drift(repo, branch)

    with pytest.raises(ValueError):
        await restore_auto_branch(repo, good_sha=good, branch="main")
    with pytest.raises(ValueError):
        auto_ref("main")

    outcome = await restore_auto_branch(repo, good_sha=good, branch=branch)
    assert outcome.status == "reverted"
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == good


# ── Interactive verify never reverts ─────────────────────────────────────


def test_verify_module_does_not_import_revert() -> None:
    import tstd.autonomy.verify as verify_mod

    tree = ast.parse(Path(verify_mod.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert not any(name == "revert" or name.endswith(".revert") for name in names)


async def test_verify_fail_does_not_revert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    branch, good = await _seed_good(repo)
    await _write_drift(repo, branch)
    drifted_tip = _git(repo, "rev-parse", f"refs/heads/{branch}")
    calls: list[str] = []

    async def _spy(*_args: object, **_kwargs: object) -> object:
        calls.append("revert")
        raise AssertionError("interactive verify must not revert")

    monkeypatch.setattr("tstd.autonomy.revert.apply_drift_result", _spy)
    monkeypatch.setattr("tstd.autonomy.revert.revert_to_last_good", _spy)
    monkeypatch.setattr("tstd.autonomy.revert.restore_auto_branch", _spy)

    session = Session(str(repo))
    session.autonomy = False
    note_tool_result(session, "fs_write", "success", diff="-good\n+drifted\n")
    mock = MockProvider(
        scripts={"test-validator": Script(kind="text", content="verdict: fail\nNo.")}
    )

    async def client_for(_cfg: object) -> MockProvider:
        return mock

    class _Assembler:
        async def assemble(self, _tier: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(text="review")

    config = make_config()
    await maybe_verify_after_turn(
        session,
        config,
        CostTracker(config),
        client_for,
        _Assembler(),  # type: ignore[arg-type]
    )
    events = [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]
    assert events and events[-1].verdict == "fail"
    assert calls == []
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "drifted\n"
    assert (repo / "extra.txt").exists()
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == drifted_tip
    assert drifted_tip != good
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before


# ── apply_drift_result unit ──────────────────────────────────────────────


async def test_skipped_check_does_not_touch_streak(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.autonomy_drift_streak = 1
    session.autonomy_last_good_sha = "abc"
    assert await apply_drift_result(session, None) is False
    assert session.autonomy_drift_streak == 1
    assert session.autonomy_last_good_sha == "abc"
    assert session.autonomy_stop_reason is None
