"""settle_ms and after_ms on the computer-use tools (TD-4847).

No desktop. Sleep and the foreground window are injected, and capture
is a fake frame, so the suite never waits and never touches a display.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp.client.client import Client

from tst_cu_mcp.capture import ScreenshotResult
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.server import build_server
from tst_cu_mcp.settle import MAX_MS, bound_ms, with_foreground

_DISPLAY = DisplayInfo(
    display_id=1,
    index=0,
    x=0,
    y=0,
    width=100,
    height=80,
    scale=1.0,
    is_main=True,
)

_ACTIONS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("click", {"x": 1, "y": 1, "coordinate_space": "points"}, "tst_cu_mcp.input_control.click"),
    ("type_text", {"text": "hi"}, "tst_cu_mcp.input_control.type_text"),
    ("press_keys", {"combo": "escape"}, "tst_cu_mcp.input_control.press_keys"),
    ("scroll", {"dy": -1}, "tst_cu_mcp.input_control.scroll"),
    ("launch_app", {"app": "Notes"}, "tst_cu_mcp.ui.launch_app"),
    (
        "ui_action",
        {"app": "Notes", "element_id": "0", "action": "press"},
        "tst_cu_mcp.ui.ui_action",
    ),
)


class Clock:
    """Records sleeps and moves ``now`` by the same amount."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _window(title: str, app: str) -> WindowInfo:
    return WindowInfo(title=title, process=app, pid=1, x=0, y=0, width=8, height=6)


def _schema(tool: Any) -> dict[str, Any]:
    raw = getattr(tool, "input_schema", None)
    if raw is None:
        raw = getattr(tool, "inputSchema", None)
    if raw is not None and hasattr(raw, "model_dump"):
        raw = raw.model_dump(by_alias=True)
    if isinstance(raw, dict) and "properties" in raw:
        return raw
    raise AssertionError(f"{tool.name} has no input schema: {tool!r}")


def _bounds(prop: dict[str, Any]) -> tuple[object, object]:
    if "minimum" in prop and "maximum" in prop:
        return prop["minimum"], prop["maximum"]
    for option in prop.get("anyOf", []):
        if isinstance(option, dict) and "minimum" in option and "maximum" in option:
            return option["minimum"], option["maximum"]
    raise AssertionError(prop)


class TestBound:
    @pytest.mark.parametrize(("value", "expected"), [(0, 0), (MAX_MS, MAX_MS)])
    def test_zero_and_the_cap(self, value: int, expected: int) -> None:
        assert bound_ms(value, name="settle_ms") == expected

    @pytest.mark.parametrize("name", ["settle_ms", "after_ms"])
    @pytest.mark.parametrize("value", [-1, MAX_MS + 1, True, False, 1.5, "250", None])
    def test_refuses_out_of_range_and_non_integers(self, name: str, value: object) -> None:
        with pytest.raises(ValueError, match=name):
            bound_ms(value, name=name)

    def test_finish_sleeps_then_reads(self) -> None:
        clock = Clock()
        seen: list[str] = []

        def _read() -> WindowInfo:
            seen.append("read")
            title = "After" if clock.sleeps else "Before"
            return _window(title, "Notes")

        def _sleep(seconds: float) -> None:
            seen.append("sleep")
            clock.sleep(seconds)

        result = with_foreground({"clicked_points": {"x": 1}}, 250, sleep=_sleep, read=_read)
        assert seen == ["sleep", "read"]
        assert clock.sleeps == [0.25]
        assert clock.now == 0.25
        assert result["foreground_window"] == {"app": "Notes", "title": "After"}
        assert result["clicked_points"] == {"x": 1}

    def test_zero_does_not_sleep(self) -> None:
        calls: list[float] = []
        result = with_foreground(
            {"pressed": "escape"},
            0,
            sleep=calls.append,
            read=lambda: _window("Before", "Old"),
        )
        assert calls == []
        assert result["foreground_window"] == {"app": "Old", "title": "Before"}

    def test_unread_window_keeps_the_action_and_hides_the_error(self) -> None:
        def _boom() -> WindowInfo:
            raise RuntimeError("secret path /Users/hidden")

        result = with_foreground({"ok": True}, 0, sleep=lambda _s: None, read=_boom)
        assert result["ok"] is True
        assert result["foreground_window"] == {"app": "", "title": "", "read": "unavailable"}
        assert "secret" not in json.dumps(result)


class TestCatalogue:
    async def test_schemas_bound_the_new_params_and_descriptions_name_them(self) -> None:
        async with Client(build_server(), mode="legacy") as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name, _args, _target in _ACTIONS:
            tool = tools[name]
            prop = _schema(tool)["properties"]["settle_ms"]
            assert _bounds(prop) == (0, MAX_MS)
            assert "settle_ms" in (tool.description or "")
            assert "foreground_window" in (tool.description or "")
        shot = tools["screenshot"]
        assert _bounds(_schema(shot)["properties"]["after_ms"]) == (0, MAX_MS)
        assert "after_ms" in (shot.description or "")
        assert "settle_ms" not in _schema(shot)["properties"]


def _patch_action(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    clock: Clock,
    state: dict[str, str],
) -> list[int]:
    """Replace the OS call and the sleeper. Returns how many times the OS was asked."""
    hits: list[int] = []

    def _act(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        hits.append(1)
        return {"ok": True}

    def _sleep(seconds: float) -> None:
        clock.sleep(seconds)
        state["title"] = "After"
        state["app"] = "Notes"

    def _read() -> WindowInfo:
        return _window(state["title"], state["app"])

    monkeypatch.setattr(target, _act)
    monkeypatch.setattr("tst_cu_mcp.settle.time.sleep", _sleep)
    monkeypatch.setattr("tst_cu_mcp.settle.foreground_window", _read)
    return hits


async def _call(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(build_server(), mode="legacy") as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error is False, result
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    pytest.fail(f"no text in {result.content!r}")


class TestActions:
    @pytest.mark.parametrize(("name", "arguments", "target"), _ACTIONS)
    async def test_settle_then_foreground(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        arguments: dict[str, Any],
        target: str,
    ) -> None:
        clock = Clock()
        state = {"title": "Before", "app": "Old"}
        hits = _patch_action(monkeypatch, target, clock, state)
        payload = await _call(name, {**arguments, "settle_ms": 250})
        assert hits == [1]
        assert clock.sleeps == [0.25]
        assert clock.now == 0.25
        assert payload["foreground_window"] == {"app": "Notes", "title": "After"}

    @pytest.mark.parametrize(("name", "arguments", "target"), _ACTIONS)
    async def test_default_does_not_wait(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        arguments: dict[str, Any],
        target: str,
    ) -> None:
        clock = Clock()
        state = {"title": "Before", "app": "Old"}
        hits = _patch_action(monkeypatch, target, clock, state)
        payload = await _call(name, arguments)
        assert hits == [1]
        assert clock.sleeps == []
        assert clock.now == 0.0
        assert payload["foreground_window"] == {"app": "Old", "title": "Before"}

    @pytest.mark.parametrize(("name", "arguments", "target"), _ACTIONS)
    @pytest.mark.parametrize("settle_ms", [-1, MAX_MS + 1])
    async def test_out_of_range_does_not_actuate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        arguments: dict[str, Any],
        target: str,
        settle_ms: int,
    ) -> None:
        clock = Clock()
        state = {"title": "Before", "app": "Old"}
        hits = _patch_action(monkeypatch, target, clock, state)
        async with Client(build_server(), mode="legacy") as client:
            result = await client.call_tool(name, {**arguments, "settle_ms": settle_ms})
        assert result.is_error is True
        assert hits == []
        assert clock.sleeps == []


def _fake_capture(seen: list[float], clock: Clock) -> Callable[..., ScreenshotResult]:
    def _capture(**_kwargs: Any) -> ScreenshotResult:
        seen.append(clock.now)
        return ScreenshotResult(
            png_bytes=b"\x89PNG\r\n\x1a\n",
            image_px_width=1,
            image_px_height=1,
            region_points=(0, 0, 100, 80),
            display=_DISPLAY,
            downscaled=False,
        )

    return _capture


class TestScreenshot:
    async def test_after_ms_delays_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = Clock()
        seen: list[float] = []
        monkeypatch.setattr("tst_cu_mcp.settle.time.sleep", clock.sleep)
        monkeypatch.setattr("tst_cu_mcp.server.capture", _fake_capture(seen, clock))
        payload = await _call("screenshot", {"after_ms": 250, "max_long_edge": 1})
        assert clock.sleeps == [0.25]
        assert seen == [0.25]
        assert "foreground_window" not in payload
        assert payload["image_px"] == {"width": 1, "height": 1}

    async def test_default_does_not_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = Clock()
        seen: list[float] = []
        monkeypatch.setattr("tst_cu_mcp.settle.time.sleep", clock.sleep)
        monkeypatch.setattr("tst_cu_mcp.server.capture", _fake_capture(seen, clock))
        payload = await _call("screenshot", {"max_long_edge": 1})
        assert clock.sleeps == []
        assert seen == [0.0]
        assert "foreground_window" not in payload
        assert "after_ms" not in payload

    @pytest.mark.parametrize("after_ms", [-1, MAX_MS + 1])
    async def test_out_of_range_does_not_capture(
        self, monkeypatch: pytest.MonkeyPatch, after_ms: int
    ) -> None:
        clock = Clock()
        seen: list[float] = []
        monkeypatch.setattr("tst_cu_mcp.settle.time.sleep", clock.sleep)
        monkeypatch.setattr("tst_cu_mcp.server.capture", _fake_capture(seen, clock))
        async with Client(build_server(), mode="legacy") as client:
            result = await client.call_tool("screenshot", {"after_ms": after_ms})
        assert result.is_error is True
        assert seen == []
        assert clock.sleeps == []
