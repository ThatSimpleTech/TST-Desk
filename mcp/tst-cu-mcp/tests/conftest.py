"""Shared test isolation for the tst-cu-mcp suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tst_cu_mcp.overlay import NullOverlay, reset_overlay, set_overlay


@pytest.fixture(autouse=True)
def isolate_overlay() -> Iterator[None]:
    """No test may spawn the real-display glow helper.

    The overlay degrades to a no-op on failure by design, but its success
    path launches an AppKit child process — exactly what a unit suite must
    never do (and CI certainly cannot). Tests that exercise overlay behavior
    install their own fake with :func:`tst_cu_mcp.overlay.set_overlay` after
    this fixture runs; :func:`reset_overlay` restores normal resolution.
    """
    set_overlay(NullOverlay())
    yield
    reset_overlay()
