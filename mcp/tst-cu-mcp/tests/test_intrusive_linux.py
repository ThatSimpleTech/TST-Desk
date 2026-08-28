"""Keyboard actuation against a real X11 window. Types where focus is.

Two independent gates, matching the Windows intrusive module:

1. this file has no ``desktop`` mark, and
2. ``TST_CU_MCP_ALLOW_INTRUSIVE_TESTS`` is checked at collection.

Run deliberately, with nothing important focused:

    TST_CU_MCP_ALLOW_INTRUSIVE_TESTS=1 pytest -m intrusive tests/test_intrusive_linux.py
"""

from __future__ import annotations

import os
import sys

import pytest

from tst_cu_mcp.backends.linux import LinuxBackend, linux_session_usable

ARM_ENV = "TST_CU_MCP_ALLOW_INTRUSIVE_TESTS"
_ARMED = os.environ.get(ARM_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

pytestmark = [
    pytest.mark.intrusive,
    pytest.mark.skipif(sys.platform != "linux", reason="native Linux desktop required"),
    pytest.mark.skipif(
        not linux_session_usable(),
        reason="native X11 session required (Wayland is unsupported)",
    ),
    pytest.mark.skipif(not _ARMED, reason=f"{ARM_ENV}=1 is required to type"),
]


def test_type_text_does_not_raise() -> None:
    LinuxBackend().type_text("tst-cu-mcp")


def test_press_keys_escape_does_not_raise() -> None:
    LinuxBackend().press_keys("escape")
