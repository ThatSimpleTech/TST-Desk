"""A local ``ui_action`` reports where the element is (TD-5227).

The frame is read before the action (a press can close or move the element)
and is best effort: anything other than a point and a size of the right
AXValue type leaves it out, and reading it never fails the action. The fake
stands in for pyobjc's ApplicationServices, so these run without macOS.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from tst_cu_mcp.backends import darwin_ax

POINT = 1
SIZE = 2
AX_VALUE_TYPE_ID = 121
AX_ERROR_NO_VALUE = -25212


class FakeAxValue:
    def __init__(self, kind: int, **fields: float) -> None:
        self.kind = kind
        self.fields = fields


class FakeAppServices:
    """The slice of pyobjc's ApplicationServices that ``_local_act`` calls."""

    kAXValueCGPointType = POINT
    kAXValueCGSizeType = SIZE

    def __init__(self, attrs: dict[str, Any]) -> None:
        self.attrs = attrs
        self.events: list[str] = []
        self.type_id_error: Exception | None = None

    def AXValueGetTypeID(self) -> int:
        return AX_VALUE_TYPE_ID

    def CFGetTypeID(self, value: Any) -> int:
        if self.type_id_error is not None:
            raise self.type_id_error
        return AX_VALUE_TYPE_ID if isinstance(value, FakeAxValue) else 7

    def AXValueGetValue(self, value: Any, kind: int, _out: None) -> tuple[bool, Any]:
        if not isinstance(value, FakeAxValue):
            # Undefined for a non-AXValue: model it as plausible garbage, so
            # only the CF type check keeps it out of the reply.
            return (True, SimpleNamespace(x=0.0, y=0.0, width=1.0, height=1.0))
        if value.kind != kind:
            return (False, None)
        return (True, SimpleNamespace(**value.fields))

    def AXUIElementCopyAttributeValue(self, _el: Any, name: str, _out: None) -> tuple[int, Any]:
        self.events.append(f"read {name}")
        if name in self.attrs:
            return (0, self.attrs[name])
        return (AX_ERROR_NO_VALUE, None)

    def AXUIElementPerformAction(self, _el: Any, action: str) -> int:
        self.events.append(f"perform {action}")
        self.attrs.clear()  # the press closed the element
        return 0

    def AXUIElementSetAttributeValue(self, _el: Any, name: str, _value: Any) -> int:
        self.events.append(f"set {name}")
        return 0


def install(monkeypatch: pytest.MonkeyPatch, attrs: dict[str, Any]) -> FakeAppServices:
    fake = FakeAppServices(attrs)
    monkeypatch.setitem(sys.modules, "ApplicationServices", cast(ModuleType, fake))
    monkeypatch.setattr(darwin_ax, "_element_at", lambda _pid, _id: object())
    return fake


def button_at(x: float, y: float, width: float, height: float) -> dict[str, Any]:
    return {
        "AXPosition": FakeAxValue(POINT, x=x, y=y),
        "AXSize": FakeAxValue(SIZE, width=width, height=height),
    }


def test_a_press_reports_the_frame_read_before_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, button_at(120.0, 48.5, 64.0, 22.0))

    result = darwin_ax._local_act(4242, "0.3.1", "press", "")

    assert result["ok"] is True
    assert result["action"] == "press"
    assert result["frame"] == {"x": 120.0, "y": 48.5, "width": 64.0, "height": 22.0}
    assert fake.events.index("perform AXPress") > fake.events.index("read AXSize")


@pytest.mark.parametrize("action", ["set_value", "focus", "raise", "show_menu"])
def test_every_action_reports_the_frame(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    install(monkeypatch, button_at(-1440.0, 10.0, 300.0, 24.0))

    result = darwin_ax._local_act(4242, "0.1", action, "hello")

    assert result["frame"] == {"x": -1440.0, "y": 10.0, "width": 300.0, "height": 24.0}


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param({"AXPosition": FakeAxValue(POINT, x=1.0, y=2.0)}, id="no-size"),
        pytest.param({"AXSize": FakeAxValue(SIZE, width=3.0, height=4.0)}, id="no-position"),
        pytest.param(
            {"AXPosition": "12,40", "AXSize": FakeAxValue(SIZE, width=3.0, height=4.0)},
            id="position-is-a-string",
        ),
        pytest.param(
            {
                "AXPosition": FakeAxValue(POINT, x=1.0, y=2.0),
                "AXSize": FakeAxValue(POINT, x=3.0, y=4.0),
            },
            id="size-is-a-point",
        ),
        pytest.param(
            {
                "AXPosition": FakeAxValue(POINT, x=float("nan"), y=2.0),
                "AXSize": FakeAxValue(SIZE, width=3.0, height=4.0),
            },
            id="not-finite",
        ),
    ],
)
def test_an_unreadable_frame_is_left_out(
    monkeypatch: pytest.MonkeyPatch, attrs: dict[str, Any]
) -> None:
    install(monkeypatch, attrs)

    result = darwin_ax._local_act(4242, "0.1", "press", "")

    assert result["ok"] is True
    assert "frame" not in result


def test_a_failing_frame_read_never_fails_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, button_at(1.0, 2.0, 3.0, 4.0))
    fake.type_id_error = RuntimeError("bridge refused the value")

    result = darwin_ax._local_act(4242, "0.1", "press", "")

    assert result["ok"] is True
    assert "frame" not in result
    assert "perform AXPress" in fake.events


def test_no_frame_without_pyobjc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ApplicationServices", None)

    assert darwin_ax._frame(object()) is None
