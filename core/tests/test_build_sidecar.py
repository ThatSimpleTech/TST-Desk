"""Sidecar smoke helpers (TD-1302 / TD-1304)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_sidecar as sidecar


def test_is_descendant_of_self() -> None:
    me = os.getpid()
    assert sidecar._is_descendant(me, me)


@pytest.mark.skipif(sys.platform == "win32", reason="uses getppid walk via ps")
def test_is_descendant_of_parent() -> None:
    me = os.getpid()
    parent = os.getppid()
    assert sidecar._is_descendant(parent, me)
    assert not sidecar._is_descendant(me, parent)


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp32 parent walk")
def test_win_parent_pid_matches_getppid() -> None:
    me = os.getpid()
    assert sidecar._win_parent_pid(me) == os.getppid()


def _timer(times: list[float]):  # type: ignore[no-untyped-def]
    """A stand-in for ``smoke`` that returns *times* in order."""
    left = list(times)
    calls: list[Path] = []

    def measure(binary: Path) -> float:
        calls.append(binary)
        return left.pop(0)

    return measure, calls


def test_a_fast_first_launch_is_the_only_launch() -> None:
    # TD-5199: a passing build is launched once.
    measure, calls = _timer([1.2, 9.0, 9.0])
    best, times = sidecar.best_startup(Path("tstd"), measure)
    assert (best, times, len(calls)) == (1.2, [1.2], 1)


def test_a_slow_cold_launch_is_tried_again() -> None:
    # A cold first launch over the budget, then a warm one under it.
    measure, calls = _timer([5.8, 4.1, 9.0])
    best, times = sidecar.best_startup(Path("tstd"), measure)
    assert (best, times, len(calls)) == (4.1, [5.8, 4.1], 2)


def test_every_launch_over_the_budget_still_fails() -> None:
    measure, _calls = _timer([5.8, 5.3, 5.4])
    best, times = sidecar.best_startup(Path("tstd"), measure)
    assert times == [5.8, 5.3, 5.4]
    assert best == 5.3 and best >= sidecar.STARTUP_BUDGET_S
