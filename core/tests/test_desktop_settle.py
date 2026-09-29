"""settle_ms / after_ms on desktop tools (TD-4847).

No real display and no real sleep. The mock driver is the foreground
window, and the clock is ``asyncio.sleep`` on the settle module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_desktop_tools import _dispatcher
from tstd.desktop.mock import MockDesktopDriver
from tstd.desktop.settle import MAX_MS, bound_ms, window_identity
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.results import ToolResult

_ACTIONS = (
    ("desktop_click", {"x": 1, "y": 2}, "click"),
    ("desktop_type", {"text": "hi"}, "type"),
    ("desktop_scroll", {"dy": -3}, "scroll"),
)


class Clock:
    """Injected sleeper. Advancing ``now`` is the only time source."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class TestBounds:
    @pytest.mark.parametrize(("value", "expected"), [(None, 0), (0, 0), (MAX_MS, MAX_MS)])
    def test_omitted_zero_and_cap_are_waits(self, value: object, expected: int) -> None:
        assert bound_ms(value, name="settle_ms") == expected

    @pytest.mark.parametrize("value", [-1, MAX_MS + 1, True, False, 1.5, "250"])
    def test_out_of_range_and_non_integers_are_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="settle_ms"):
            bound_ms(value, name="settle_ms")

    def test_process_name_is_the_app(self) -> None:
        assert window_identity({"process": "Notes", "title": "Untitled", "pid": 4}) == {
            "app": "Notes",
            "title": "Untitled",
        }


class TestSchemas:
    def test_action_schemas_bound_settle_ms_and_name_it(self) -> None:
        registry = create_registry()
        for name, _args, _call in _ACTIONS:
            tool = registry.get(name)
            assert tool is not None
            prop = tool.parameters["properties"]["settle_ms"]
            assert prop["minimum"] == 0
            assert prop["maximum"] == MAX_MS
            assert "settle_ms" in tool.description
            assert "foreground_window" in tool.description

    def test_screenshot_schema_bounds_after_ms_and_names_it(self) -> None:
        tool = create_registry().get("desktop_screenshot")
        assert tool is not None
        prop = tool.parameters["properties"]["after_ms"]
        assert prop["minimum"] == 0
        assert prop["maximum"] == MAX_MS
        assert "after_ms" in tool.description
        assert "settle_ms" not in tool.parameters["properties"]

    def test_move_is_unchanged(self) -> None:
        tool = create_registry().get("desktop_move")
        assert tool is not None
        assert "settle_ms" not in tool.parameters["properties"]
        assert "after_ms" not in tool.parameters["properties"]


def _open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: MockDesktopDriver
) -> tuple[ToolDispatcher, Clock]:
    clock = Clock()
    monkeypatch.setattr("tstd.desktop.settle.asyncio.sleep", clock.sleep)
    return _dispatcher(tmp_path, driver)[0], clock


class TestActions:
    @pytest.mark.parametrize(("name", "arguments", "call"), _ACTIONS)
    async def test_settle_reads_the_window_after_the_wait(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        arguments: dict[str, object],
        call: str,
    ) -> None:
        driver = MockDesktopDriver(foreground_title="Before", foreground_app="Old")
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)

        def _swap(_seconds: float) -> None:
            driver.foreground_title = "After"
            driver.foreground_app = "Notes"

        clock.on_sleep = _swap
        result = await dispatcher.dispatch("c1", name, {**arguments, "settle_ms": 250})
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["foreground_window"] == {"app": "Notes", "title": "After"}
        assert clock.sleeps == [0.25]
        assert clock.now == 0.25
        assert driver.actuations == [call]
        if name == "desktop_type":
            assert "hi" not in result.output

    @pytest.mark.parametrize(("name", "arguments", "call"), _ACTIONS)
    async def test_default_does_not_wait_and_still_names_the_window(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        arguments: dict[str, object],
        call: str,
    ) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch("c1", name, arguments)
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["foreground_window"] == {"app": "mock", "title": "Mock Window"}
        assert clock.sleeps == []
        assert clock.now == 0.0
        assert driver.actuations == [call]

    async def test_cap_is_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 1, "y": 2, "settle_ms": MAX_MS}
        )
        assert result.status == "success"
        assert clock.sleeps == [MAX_MS / 1000.0]

    @pytest.mark.parametrize("value", [-1, MAX_MS + 1, True, 1.5, "250"])
    async def test_bad_settle_does_not_actuate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: object,
    ) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 1, "y": 2, "settle_ms": value}
        )
        assert result.status == "error"
        assert result.error_code == "invalid_arguments"
        assert driver.actuations == []
        assert clock.sleeps == []

    async def test_focus_refusal_does_not_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = MockDesktopDriver(foreground_title="Terminal", foreground_app="zsh")
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch(
            "c1",
            "desktop_click",
            {"x": 1, "y": 2, "expect_window": "Chrome", "settle_ms": 250},
        )
        assert result.status == "error"
        assert result.error_code == "focus_mismatch"
        assert driver.actuations == []
        assert clock.sleeps == []

    async def test_unread_window_does_not_fail_the_action_or_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Unread(MockDesktopDriver):
            async def foreground_window(self) -> dict[str, Any]:
                raise RuntimeError("secret path /Users/hidden/window")

        driver = _Unread()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch(
            "c1", "desktop_click", {"x": 1, "y": 2, "settle_ms": 250}
        )
        assert isinstance(result, ToolResult)
        assert result.status == "success"
        assert json.loads(result.output)["foreground_window"] == {
            "app": "",
            "title": "",
            "read": "unavailable",
        }
        assert "secret" not in result.output
        assert "/Users/hidden" not in result.output
        assert driver.actuations == ["click"]
        assert clock.sleeps == [0.25]


class TestScreenshot:
    async def test_after_ms_delays_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        # The driver records the capture. A wait that ran first sees no
        # capture yet; the clock has moved when the call returns.
        seen: list[int] = []

        def _mark(_seconds: float) -> None:
            seen.append(len(driver.calls))

        clock.on_sleep = _mark
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {"after_ms": 250})
        assert result.status == "success"
        assert clock.sleeps == [0.25]
        assert clock.now == 0.25
        assert seen == [0]
        assert [name for name, _kwargs in driver.calls] == ["screenshot"]
        assert driver.actuations == []

    async def test_default_capture_does_not_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        assert result.status == "success"
        assert clock.sleeps == []
        assert clock.now == 0.0
        assert [name for name, _kwargs in driver.calls] == ["screenshot"]

    @pytest.mark.parametrize("value", [-1, MAX_MS + 1, True, "20"])
    async def test_bad_after_ms_does_not_capture(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: object,
    ) -> None:
        driver = MockDesktopDriver()
        dispatcher, clock = _open(tmp_path, monkeypatch, driver)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {"after_ms": value})
        assert result.status == "error"
        assert result.error_code == "invalid_arguments"
        assert driver.calls == []
        assert clock.sleeps == []
