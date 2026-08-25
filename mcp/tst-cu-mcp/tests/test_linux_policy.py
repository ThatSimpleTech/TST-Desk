"""Kill-switch and expect_window against LinuxBackend spies.

Policy lives in input_control. These tests prove a new platform cannot skip it:
the assertion is that LinuxBackend was never reached on a refuse.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import input_control, safety
from tst_cu_mcp.backends.linux import LinuxBackend
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowFocusError, WindowInfo

ONE_DISPLAY = [
    DisplayInfo(display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True)
]

TERMINAL = WindowInfo(
    title="Terminal",
    process="xfce4-terminal",
    pid=42,
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
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(input_control, "list_displays", lambda: ONE_DISPLAY)
    monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: TERMINAL)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def spy(name: str) -> Callable[..., None]:
        def _inner(*_args: Any, **_kwargs: Any) -> None:
            seen.append(name)

        return _inner

    for method in ("move_mouse", "click", "type_text", "press_keys", "scroll"):
        monkeypatch.setattr(LinuxBackend, method, spy(method))
    return seen


WRONG: dict[str, Callable[[], None]] = {
    "move_mouse": lambda: input_control.move_mouse(10, 10, expect_window="Chrome"),
    "click": lambda: input_control.click(10, 10, expect_window="Chrome"),
    "type_text": lambda: input_control.type_text("hi", expect_window="Chrome"),
    "press_keys": lambda: input_control.press_keys("ctrl+c", expect_window="Chrome"),
    "scroll": lambda: input_control.scroll(0, 3, expect_window="Chrome"),
}


class TestExpectWindowRefusesWithoutActuating:
    @pytest.mark.parametrize("name", sorted(WRONG))
    def test_mismatch_never_reaches_the_backend(self, name: str, reached: list[str]) -> None:
        with pytest.raises(WindowFocusError):
            WRONG[name]()
        assert reached == []

    def test_match_reaches_the_backend(self, reached: list[str]) -> None:
        input_control.move_mouse(10, 10, expect_window="Terminal")
        assert reached == ["move_mouse"]


class TestKillSwitchRefusesWithoutActuating:
    @pytest.mark.parametrize("name", sorted(WRONG))
    def test_stop_file_blocks_every_action(
        self, name: str, tmp_path: Path, reached: list[str]
    ) -> None:
        (tmp_path / "STOP").write_text("")
        with pytest.raises(safety.KillSwitchEngaged):
            WRONG[name]()
        assert reached == []
