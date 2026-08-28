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
