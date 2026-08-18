"""The expect_window guard, checked at the policy layer for every input tool.

The assertion that matters throughout is not merely that a mismatch raises — it
is that the backend was never reached. A guard that refuses *after* actuating is
worse than none, because it reports failure for something that happened.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import input_control, safety
from tst_cu_mcp.backends.windows import WindowsBackend
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowFocusError, WindowInfo

ONE_DISPLAY = [
    DisplayInfo(display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True)
]

NOTEPAD = WindowInfo(
    title="Untitled - Notepad",
    process="notepad.exe",
    pid=11,
    x=0,
    y=0,
    width=800,
    height=600,
)


@pytest.fixture(autouse=True)
def isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(None)
    yield
    safety.set_config(None)


@pytest.fixture(autouse=True)
def windows_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(input_control, "list_displays", lambda: ONE_DISPLAY)
    monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: NOTEPAD)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record backend actuation. An empty list is the real assertion."""
    seen: list[str] = []

    def spy(name: str) -> Callable[..., None]:
        def _inner(*_args: Any, **_kwargs: Any) -> None:
            seen.append(name)

        return _inner

    for method in ("move_mouse", "click", "type_text", "press_keys", "scroll"):
        monkeypatch.setattr(WindowsBackend, method, spy(method))
    return seen


# Every input entry point, called with a deliberately wrong expectation.
WRONG: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10, expect_window="Chrome"),
    "click": lambda: input_control.click(10, 10, expect_window="Chrome"),
    "type_text": lambda: input_control.type_text("hi", expect_window="Chrome"),
    "press_keys": lambda: input_control.press_keys("ctrl+c", expect_window="Chrome"),
    "scroll": lambda: input_control.scroll(0, 3, expect_window="Chrome"),
}

RIGHT: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10, expect_window="Notepad"),
    "click": lambda: input_control.click(10, 10, expect_window="Notepad"),
    "type_text": lambda: input_control.type_text("hi", expect_window="Notepad"),
    "press_keys": lambda: input_control.press_keys("ctrl+c", expect_window="Notepad"),
    "scroll": lambda: input_control.scroll(0, 3, expect_window="Notepad"),
}

UNGUARDED: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10),
    "click": lambda: input_control.click(10, 10),
    "type_text": lambda: input_control.type_text("hi"),
    "press_keys": lambda: input_control.press_keys("ctrl+c"),
    "scroll": lambda: input_control.scroll(0, 3),
}


class TestMismatchRefusesBeforeActuating:
    @pytest.mark.parametrize("name", sorted(WRONG))
    def test_every_tool_refuses(self, name: str, reached: list[str]) -> None:
        with pytest.raises(WindowFocusError):
            WRONG[name]()
        assert reached == []


class TestMatchProceeds:
    @pytest.mark.parametrize("name", sorted(RIGHT))
    def test_every_tool_acts(self, name: str, reached: list[str]) -> None:
        RIGHT[name]()
        assert name in reached

    @pytest.mark.parametrize("name", sorted(RIGHT))
    def test_matching_on_the_process_name_also_works(self, name: str, reached: list[str]) -> None:
        input_control.move_mouse(10, 10, expect_window="notepad.exe")
        assert reached == ["move_mouse"]


class TestGuardIsOptional:
    @pytest.mark.parametrize("name", sorted(UNGUARDED))
    def test_omitting_it_keeps_the_old_behaviour(self, name: str, reached: list[str]) -> None:
        # The guard is additive. Every existing caller must keep working.
        UNGUARDED[name]()
        assert name in reached

    def test_omitting_it_costs_no_os_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        def spy() -> WindowInfo:
            nonlocal called
            called = True
            return NOTEPAD

        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", spy)
        monkeypatch.setattr(WindowsBackend, "move_mouse", lambda *_a, **_k: None)
        input_control.move_mouse(10, 10)
        assert called is False


class TestOrderOfChecks:
    """The kill-switch outranks the guard, and argument validation outranks both."""

    def test_kill_switch_wins_over_a_focus_mismatch(
        self, tmp_path: Path, reached: list[str]
    ) -> None:
        # Both would refuse. "Stop" must not depend on anything else being right,
        # so its message is the one that surfaces.
        (tmp_path / "STOP").write_text("")
        with pytest.raises(safety.KillSwitchEngaged):
            input_control.click(10, 10, expect_window="Chrome")
        assert reached == []

    def test_bad_arguments_beat_a_focus_match(self, reached: list[str]) -> None:
        # A malformed call should fail on its own merits rather than on whatever
        # happened to be in front.
        with pytest.raises(ValueError, match="unknown button"):
            input_control.click(10, 10, button="middle", expect_window="Notepad")
        assert reached == []

    def test_offscreen_beats_a_focus_match(self, reached: list[str]) -> None:
        with pytest.raises(ValueError, match="off-screen"):
            input_control.move_mouse(99999, 10, expect_window="Notepad")
        assert reached == []

    def test_bad_combo_beats_a_focus_match(self, reached: list[str]) -> None:
        with pytest.raises(ValueError, match="macOS-only"):
            input_control.press_keys("fn+up", expect_window="Notepad")
        assert reached == []


class TestCursorPositionIsARead:
    def test_delegates_to_the_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WindowsBackend, "cursor_position", lambda _s: (7, 9))
        assert input_control.cursor_position() == (7, 9)

    def test_works_while_the_kill_switch_is_engaged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Halting the hands must not blind the eyes: a caller that has just been
        # refused still needs to see where things stand.
        (tmp_path / "STOP").write_text("")
        monkeypatch.setattr(WindowsBackend, "cursor_position", lambda _s: (1, 2))
        assert input_control.cursor_position() == (1, 2)
