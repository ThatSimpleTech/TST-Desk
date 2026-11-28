"""Clicks land where the model pointed on the screenshot it was shown (CU-10).

tst-cu-mcp downscales a capture (1568 px long edge; the packaged Linux
driver 768) and the model answers in that image's pixels, but every click,
move and scroll went out as global logical points, so on any display wider
than the image it landed off by the scale factor.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from tstd.desktop.inprocess import InProcessDesktopDriver
from tstd.desktop.mcp_driver import McpDesktopDriver

META = {
    "image_px": {"width": 1568, "height": 980},
    "captured_region_points": {"x": 0, "y": 0, "width": 3136, "height": 1960},
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        if name == "screenshot":
            return {
                "content": [
                    {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"},
                    {"type": "text", "text": json.dumps(META)},
                ]
            }
        return {"content": [{"type": "text", "text": "{}"}]}


async def test_before_a_screenshot_a_point_is_a_screen_point() -> None:
    client = FakeClient()
    driver = McpDesktopDriver(["tst-cu-mcp"], platform="darwin", client=client)  # type: ignore[arg-type]
    await driver.click(10, 20)
    assert client.calls[-1][1]["coordinate_space"] == "points"


async def test_after_a_screenshot_a_point_is_in_its_pixels() -> None:
    client = FakeClient()
    driver = McpDesktopDriver(["tst-cu-mcp"], platform="darwin", client=client)  # type: ignore[arg-type]
    await driver.screenshot()
    await driver.click(784, 490)
    args = client.calls[-1][1]
    assert args["coordinate_space"] == "image"
    assert (args["image_width"], args["image_height"]) == (1568, 980)
    assert args["region"] == {"x": 0, "y": 0, "width": 3136, "height": 1960}
    await driver.scroll(dy=3, x=10, y=10)
    assert client.calls[-1][1]["coordinate_space"] == "image"


@pytest.fixture
def fake_cu(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    clicked: list[tuple[float, float]] = []
    pkg = types.ModuleType("tst_cu_mcp")
    capture = types.ModuleType("tst_cu_mcp.capture")
    coords = types.ModuleType("tst_cu_mcp.coordinates")
    control = types.ModuleType("tst_cu_mcp.input_control")

    class Result:
        png_bytes = b"\\x89PNG"
        image_px_width, image_px_height = 768, 480
        region_points = (0, 0, 1920, 1200)

    capture.capture = lambda **_kw: Result()  # type: ignore[attr-defined]

    def to_point(region: Any, w: int, h: int, x: float, y: float) -> tuple[float, float]:
        if not (0 <= x <= w and 0 <= y <= h):
            raise ValueError("outside the image")
        return region[0] + x * region[2] / w, region[1] + y * region[3] / h

    coords.image_px_to_global_point = to_point  # type: ignore[attr-defined]
    control.click = lambda x, y, *_a: clicked.append((x, y))  # type: ignore[attr-defined]
    for name, mod in {
        "tst_cu_mcp": pkg,
        "tst_cu_mcp.capture": capture,
        "tst_cu_mcp.coordinates": coords,
        "tst_cu_mcp.input_control": control,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return clicked


async def test_the_linux_driver_maps_its_768px_frame(fake_cu: list[tuple[float, float]]) -> None:
    driver = InProcessDesktopDriver()
    await driver.screenshot()
    await driver.click(384, 240)
    assert fake_cu == [(960.0, 600.0)]  # the screen's centre, not (384, 240)
