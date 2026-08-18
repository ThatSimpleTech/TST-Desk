r"""Keyboard and scroll actuation against a real window. Types where focus is.

**These tests send real keystrokes into whichever window currently has focus.**
There is no way to make that safe, only deliberate. They exist because the
alternative is worse: without them, ``type_text`` and ``press_keys`` rest
entirely on unit tests of the code that *builds* the INPUT records, and those
cannot prove the OS accepted one.

Two independent gates, because one was not enough
-------------------------------------------------

These tests previously lived in ``test_desktop_windows.py`` with only a
``@pytest.mark.intrusive`` decorator. That was not sufficient, and the failure is
worth recording where the next person will read it:

* the module applied ``pytest.mark.desktop`` to everything in it via
  ``pytestmark``, so these tests carried *both* marks;
* a command-line ``-m desktop`` **replaces** the ``addopts`` filter
  ``-m 'not desktop and not intrusive'`` rather than intersecting with it.

So ``pytest -m desktop`` selected them. They typed ``tst-cu-mcp`` and
``café 😀`` into a chat window three times before it was spotted.

The lesson is that a marker is a *selector*, not a guard. So this file now has:

1. its own module with no ``desktop`` mark anywhere, and
2. a hard environment-variable gate, checked at collection.

Even ``pytest -m intrusive`` skips unless the variable is set. Run them
deliberately, with nothing important focused:

.. code-block:: powershell

    $env:TST_CU_MCP_ALLOW_INTRUSIVE_TESTS = "1"
    .\.venv\Scripts\python.exe -m pytest -m intrusive
    Remove-Item Env:\TST_CU_MCP_ALLOW_INTRUSIVE_TESTS
"""

from __future__ import annotations

import os
import sys

import pytest

from tst_cu_mcp.backends.windows import WindowsBackend

#: Set to an affirmative value to arm these tests. Absent means skip, always.
ARM_ENV = "TST_CU_MCP_ALLOW_INTRUSIVE_TESTS"
_ARMED = os.environ.get(ARM_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

pytestmark = [
    pytest.mark.intrusive,
    pytest.mark.skipif(sys.platform != "win32", reason="native Windows desktop required"),
    pytest.mark.skipif(
        not _ARMED,
        reason=(
            f"sends real keystrokes to the focused window; set {ARM_ENV}=1 to arm. "
            "Deliberately not selectable by marker alone."
        ),
    ),
]


@pytest.fixture
def backend() -> WindowsBackend:
    return WindowsBackend()


class TestKeyboardIntoARealWindow:
    """Proves the OS accepted synthesized input, which only the OS can confirm.

    ``_send`` raises when ``SendInput`` reports fewer events delivered than were
    submitted, so a clean return means the keystrokes genuinely reached the input
    queue. It does *not* prove the receiving window processed them — a window that
    has just taken focus can still discard them, which is why the README tells
    callers to screenshot and confirm a caret rather than trusting a success.
    """

    def test_typing_reports_success(self, backend: WindowsBackend) -> None:
        backend.type_text("tst-cu-mcp")

    def test_unicode_typing_reports_success(self, backend: WindowsBackend) -> None:
        # Astral-plane character: exercises the surrogate-pair path in utf16_units
        # against a real keyboard queue rather than a unit test's expectation.
        backend.type_text("café \U0001f600")

    def test_key_combo_reports_success(self, backend: WindowsBackend) -> None:
        # Deliberately inert: Ctrl+F6 does nothing in most applications.
        backend.press_keys("ctrl+f6")

    def test_windows_key_alone_reports_success(self, backend: WindowsBackend) -> None:
        # The TD gap found by live use: VK_LWIN needs the extended flag to reach
        # Start. Opens the Start menu, then closes it again.
        backend.press_keys("win")
        backend.press_keys("escape")

    def test_scroll_reports_success(self, backend: WindowsBackend) -> None:
        backend.scroll(0, 1)
        backend.scroll(0, -1)


class TestTheGateItself:
    """The gate has to be provable, or it is just another comment."""

    def test_arming_variable_is_what_let_these_run(self) -> None:
        # If this file is executing, the variable was set. Reading it back is the
        # only way to assert the gate is the thing gating, rather than a marker
        # that some other selection could bypass.
        assert _ARMED is True
        assert os.environ.get(ARM_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

    def test_no_desktop_marker_on_this_module(self) -> None:
        # The specific regression. A `desktop` mark here would make
        # `pytest -m desktop` type into someone's window again.
        names = {mark.name for mark in pytestmark if hasattr(mark, "name")}
        assert "desktop" not in names
        assert "intrusive" in names
