"""Observe-only Design-mode hit-test (TD-3406).

Never moves the pointer. The kill-switch must not block this path —
same contract as screenshot.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tst_cu_mcp.backends.darwin import DarwinBackend
from tst_cu_mcp.backends.linux import LinuxBackend
from tst_cu_mcp.backends.windows import WindowsBackend
from tst_cu_mcp.capture import ScreenshotResult
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.hit_test import (
    clear_capture,
    empty_node,
    global_box_to_image,
    observe_at,
    remember_capture,
)


class _ScriptedBackend:
    def __init__(self) -> None:
        self.seen: list[tuple[float, float]] = []
        self.node: dict[str, Any] = {
            "role": "AXButton",
            "attributes": {"AXTitle": "Ok"},
            "box": {"x": 100.0, "y": 200.0, "width": 80.0, "height": 24.0},
        }

    def hit_test(self, x: float, y: float) -> dict[str, Any]:
        self.seen.append((x, y))
        return self.node


@pytest.fixture(autouse=True)
def _reset_capture() -> Iterator[None]:
    clear_capture()
    yield
    clear_capture()


def test_kill_switch_is_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("hit_test must not call the kill-switch")

    monkeypatch.setattr("tst_cu_mcp.safety.ensure_actuation_allowed", boom)
    fake = _ScriptedBackend()
    monkeypatch.setattr("tst_cu_mcp.backends.get_backend", lambda: fake)
    node = observe_at(10.0, 20.0, coordinate_space="points")
    assert node["role"] == "AXButton"
    assert fake.seen == [(10.0, 20.0)]


def test_image_space_uses_last_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ScriptedBackend()
    monkeypatch.setattr("tst_cu_mcp.backends.get_backend", lambda: fake)
    remember_capture(
        ScreenshotResult(
            png_bytes=b"\x89PNG",
            image_px_width=100,
            image_px_height=50,
            region_points=(0, 0, 200, 100),
            display=DisplayInfo(
                display_id=1,
                index=0,
                x=0,
                y=0,
                width=200,
                height=100,
                scale=1.0,
                is_main=True,
            ),
            downscaled=False,
        )
    )
    node = observe_at(50.0, 25.0, coordinate_space="image")
    assert fake.seen == [(100.0, 50.0)]
    assert node["box"] == {"x": 50.0, "y": 100.0, "width": 40.0, "height": 12.0}


def test_global_box_maps_onto_the_frozen_frame() -> None:
    mapped = global_box_to_image(
        (10, 20, 100, 50),
        200,
        100,
        {"x": 20, "y": 30, "width": 10, "height": 5},
    )
    assert mapped == {"x": 20.0, "y": 20.0, "width": 20.0, "height": 10.0}


def test_empty_node_is_a_miss() -> None:
    node = empty_node(3.0, 4.0)
    assert node["role"] is None
    assert node["xpath"] is None
    assert node["box"]["x"] == 3.0


def test_backends_import_on_this_host() -> None:
    DarwinBackend()
    WindowsBackend()
    LinuxBackend()
    for backend in (DarwinBackend(), WindowsBackend(), LinuxBackend()):
        assert callable(backend.hit_test)
