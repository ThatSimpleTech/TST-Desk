"""Frozen-aware overlay helper spawn (TD-4834).

A checkout spawns ``python -m tst_cu_mcp.overlay.darwin_helper``; the
frozen sidecar has no ``-m`` and must re-exec itself with ``--cu-overlay``.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tst_cu_mcp.overlay import darwin


class _Proc:
    """Popen stand-in: PipeTransport only stores it."""

    def __init__(self, argv: list[str], **_kwargs: Any) -> None:
        self.argv = argv
        self.stdin = None
        self.stdout = None


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> _Proc:
        seen.append(argv)
        return _Proc(argv, **kwargs)

    monkeypatch.setattr(darwin.subprocess, "Popen", fake_popen)
    return seen


def test_checkout_spawns_module(spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    darwin.default_spawn()
    assert spawned[0][1:] == ["-m", "tst_cu_mcp.overlay.darwin_helper"]


def test_frozen_reexecs_with_flag(spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    darwin.default_spawn()
    assert spawned[0][1:] == ["--cu-overlay"]
    assert spawned[0][0] == sys.executable
