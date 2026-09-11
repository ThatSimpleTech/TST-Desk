"""Shared test isolation for the tst-cu-mcp suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tst_cu_mcp.overlay import NullOverlay, reset_overlay, set_overlay
from tst_cu_mcp.server import INTERNAL_ENV


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


@pytest.fixture(autouse=True)
def hide_internal_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to the surface a model sees.

    ``TST_CU_MCP_INTERNAL`` is the daemon's own flag. A developer who exports it
    would otherwise get a different tool list from the one CI asserts; the tests
    that mean to be the daemon set it themselves.
    """
    monkeypatch.delenv(INTERNAL_ENV, raising=False)
