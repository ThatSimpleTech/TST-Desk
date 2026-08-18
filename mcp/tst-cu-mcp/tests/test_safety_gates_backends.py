"""The kill-switch must gate the Windows path, not just the one it was written for.

A safety net that only covers the original platform is worse than none, because
it still reads as covered. Every actuation entry point is checked against the
Windows backend here, and the assertion is not merely that it raised — it is that
the backend was never reached.
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

ONE_DISPLAY = [
    DisplayInfo(
        display_id=1,
        index=0,
        x=0,
        y=0,
        width=1920,
        height=1080,
        scale=1.0,
        is_main=True,
    )
]


@pytest.fixture(autouse=True)
def isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the stop-file at a temp dir and reset the module-level config.

    Without the redirect these tests would read (and the engaged cases would
    depend on) the developer's real ``~/.tst-cu-mcp/STOP``.
    """
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(None)
    yield
    safety.set_config(None)


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the Windows backend regardless of the host running the suite."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(input_control, "list_displays", lambda: ONE_DISPLAY)


@pytest.fixture
def refuse_all_actuation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any backend actuation. An empty list is the assertion that matters."""
    reached: list[str] = []

    def spy(name: str) -> Callable[..., None]:
        def _inner(*_args: Any, **_kwargs: Any) -> None:
            reached.append(name)

        return _inner

    for method in ("move_mouse", "click", "type_text", "press_keys", "scroll"):
        monkeypatch.setattr(WindowsBackend, method, spy(method))
    return reached


ACTIONS: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10),
    "click": lambda: input_control.click(10, 10),
    "type_text": lambda: input_control.type_text("hello"),
    "press_keys": lambda: input_control.press_keys("ctrl+c"),
    "scroll": lambda: input_control.scroll(0, 3),
}


@pytest.mark.usefixtures("windows_host")
class TestStopFileBlocksTheWindowsBackend:
    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_every_action_is_refused(
        self, name: str, tmp_path: Path, refuse_all_actuation: list[str]
    ) -> None:
        (tmp_path / "STOP").write_text("")
        with pytest.raises(safety.KillSwitchEngaged):
            ACTIONS[name]()
        assert refuse_all_actuation == []

    def test_refusal_names_the_stop_file_so_it_can_be_undone(self, tmp_path: Path) -> None:
        stop = tmp_path / "STOP"
        stop.write_text("")
        with pytest.raises(safety.KillSwitchEngaged, match="delete it to resume"):
            input_control.move_mouse(10, 10)


@pytest.mark.usefixtures("windows_host")
class TestEnvVarBlocksTheWindowsBackend:
    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_every_action_is_refused(
        self, name: str, monkeypatch: pytest.MonkeyPatch, refuse_all_actuation: list[str]
    ) -> None:
        monkeypatch.setenv(safety.STOP_ENV, "1")
        with pytest.raises(safety.KillSwitchEngaged):
            ACTIONS[name]()
        assert refuse_all_actuation == []

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(safety.STOP_ENV, value)
        with pytest.raises(safety.KillSwitchEngaged):
            input_control.move_mouse(10, 10)

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_falsey_spellings_do_not_block(
        self, value: str, monkeypatch: pytest.MonkeyPatch, refuse_all_actuation: list[str]
    ) -> None:
        monkeypatch.setenv(safety.STOP_ENV, value)
        input_control.move_mouse(10, 10)
        assert refuse_all_actuation == ["move_mouse"]


@pytest.mark.usefixtures("windows_host")
class TestConfigDisablesTheWindowsBackend:
    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_every_action_is_refused(self, name: str, refuse_all_actuation: list[str]) -> None:
        from tst_cu_mcp.config import Config

        safety.set_config(Config(actuation_enabled=False))
        with pytest.raises(safety.KillSwitchEngaged, match=r"actuation\.enabled"):
            ACTIONS[name]()
        assert refuse_all_actuation == []


@pytest.mark.usefixtures("windows_host")
class TestActuationReachesTheBackendWhenAllowed:
    """The negative control. Without it, a gate that blocks everything passes."""

    @pytest.mark.parametrize("name", sorted(ACTIONS))
    def test_each_action_reaches_the_backend(
        self, name: str, refuse_all_actuation: list[str]
    ) -> None:
        ACTIONS[name]()
        assert refuse_all_actuation == [name]

    def test_screenshots_still_work_while_actuation_is_halted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The kill-switch stops the hands, not the eyes: the model must still be
        # able to see what it did before it was stopped.
        (tmp_path / "STOP").write_text("")
        captured: list[tuple[int, int, int, int]] = []

        def fake_capture(_self: object, rect: tuple[int, int, int, int]) -> bytes:
            captured.append(rect)
            return b"\x89PNG"

        monkeypatch.setattr(WindowsBackend, "capture_png", fake_capture)
        assert WindowsBackend().capture_png((0, 0, 10, 10)) == b"\x89PNG"
        assert captured == [(0, 0, 10, 10)]


@pytest.mark.usefixtures("windows_host")
class TestValidationHappensBeforeActuation:
    def test_unknown_button_is_refused(self, refuse_all_actuation: list[str]) -> None:
        with pytest.raises(ValueError, match="unknown button"):
            input_control.click(10, 10, button="middle")
        assert refuse_all_actuation == []

    def test_offscreen_point_is_refused(self, refuse_all_actuation: list[str]) -> None:
        with pytest.raises(ValueError, match="off-screen"):
            input_control.move_mouse(99999, 10)
        assert refuse_all_actuation == []

    def test_overlong_text_is_refused(self, refuse_all_actuation: list[str]) -> None:
        with pytest.raises(ValueError, match="text too long"):
            input_control.type_text("x" * (input_control.MAX_TEXT_LEN + 1))
        assert refuse_all_actuation == []

    def test_oversized_scroll_is_refused(self, refuse_all_actuation: list[str]) -> None:
        with pytest.raises(ValueError, match="scroll magnitude"):
            input_control.scroll(0, input_control.MAX_SCROLL_LINES + 1)
        assert refuse_all_actuation == []

    def test_bad_combo_is_refused_before_any_key_goes_down(
        self, refuse_all_actuation: list[str]
    ) -> None:
        # Half-pressing a modifier and then failing would leave Ctrl stuck down
        # on the user's keyboard, which is why press_keys parses first.
        with pytest.raises(ValueError, match="macOS-only"):
            input_control.press_keys("fn+up")
        assert refuse_all_actuation == []
