"""Desktop computer-use tools (TD-3301).

Registered on the session dispatcher. Path/host fields stay empty so
PathGuard never runs. Classification comes from ``Tool.actuates``.
"""

from __future__ import annotations

import base64
import json
from functools import partial
from typing import TYPE_CHECKING

from ..cu_indicators import hide_real_display_for_screenshot
from ..desktop import DesktopDriver, DesktopError
from ..desktop.grounding_client import GroundingLocator, GroundingResult
from ..screen.frames import persist_screen_frame
from .registry import Tool, ToolRegistry

if TYPE_CHECKING:
    from .dispatch import ToolDispatcher

_EXPECT_WINDOW = {
    "type": "string",
    "description": (
        "Refuse without actuating unless the foreground window title or "
        "process name contains this substring (case-insensitive)."
    ),
}


def register_desktop_tools(registry: ToolRegistry) -> None:
    """Add the five desktop tools. Call from ``create_registry``."""
    registry.register(
        Tool(
            name="desktop_screenshot",
            description=(
                "Capture the desktop and return a PNG as base64. This cannot "
                "move the pointer or type. Optional display is a 0-based index."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "display": {
                        "type": "integer",
                        "description": "Display index; omit for the main display",
                    },
                },
            },
            side_effect_class="auto",
            parallel_safe=True,
            mutates=False,
            actuates=False,
        )
    )
    registry.register(
        Tool(
            name="desktop_move",
            description="Move the desktop pointer to a point (global coordinates).",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Horizontal coordinate"},
                    "y": {"type": "number", "description": "Vertical coordinate"},
                    "expect_window": _EXPECT_WINDOW,
                },
                "required": ["x", "y"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="desktop_click",
            description="Click the desktop pointer at a point (global coordinates).",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Horizontal coordinate"},
                    "y": {"type": "number", "description": "Vertical coordinate"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right"],
                        "description": "Mouse button (default left)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Click count; 2 is a double-click",
                        "default": 1,
                    },
                    "expect_window": _EXPECT_WINDOW,
                    "target": {
                        "type": "string",
                        "description": (
                            "UI element to ground on a screenshot when a local "
                            "grounding model is configured. Omitted uses the "
                            "intended x,y as the phrase."
                        ),
                    },
                },
                "required": ["x", "y"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="desktop_type",
            description=(
                "Type Unicode at the current keyboard focus. Does not move "
                "focus — click a field first. The typed text is never logged."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "expect_window": _EXPECT_WINDOW,
                },
                "required": ["text"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="desktop_scroll",
            description=(
                "Scroll the desktop. dy>0 scrolls up, dy<0 down; dx is "
                "horizontal. Optional x,y moves the pointer first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dx": {"type": "integer", "description": "Horizontal scroll", "default": 0},
                    "dy": {"type": "integer", "description": "Vertical scroll", "default": 0},
                    "x": {"type": "number", "description": "Optional pointer x before scrolling"},
                    "y": {"type": "number", "description": "Optional pointer y before scrolling"},
                    "expect_window": _EXPECT_WINDOW,
                },
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )


def _png_from_driver_json(raw: str) -> bytes | None:
    """Pull PNG bytes from a successful driver screenshot payload."""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    b64 = body.get("png_base64") if isinstance(body, dict) else None
    if not isinstance(b64, str) or not b64:
        return None
    try:
        png = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return None
    return png if png.startswith(b"\x89PNG") else None


async def desktop_screenshot(
    session: object,
    driver: DesktopDriver,
    display: int | None = None,
    tool_call_id: str = "",
) -> str:
    with hide_real_display_for_screenshot():
        raw = await driver.screenshot(display=display)
    png = _png_from_driver_json(raw)
    if png is not None:
        await persist_screen_frame(session, png, tool_call_id=tool_call_id or None)
    return raw


async def desktop_move(
    session: object,
    x: float,
    y: float,
    driver: DesktopDriver,
    expect_window: str | None = None,
    tool_call_id: str = "",
) -> str:
    return await driver.move(x=x, y=y, expect_window=expect_window)


async def desktop_click(
    session: object,
    x: float,
    y: float,
    driver: DesktopDriver,
    button: str = "left",
    count: int = 1,
    expect_window: str | None = None,
    target: str | None = None,
    grounding_client: GroundingLocator | None = None,
    tool_call_id: str = "",
) -> str:
    aimed_x, aimed_y, grounding = await aim_desktop_click(
        driver,
        x,
        y,
        target=target or "",
        client=grounding_client,
    )
    raw = await driver.click(
        x=aimed_x,
        y=aimed_y,
        button=button,
        count=count,
        expect_window=expect_window,
    )
    return _with_grounding(raw, grounding)


async def desktop_type(
    session: object,
    text: str,
    driver: DesktopDriver,
    expect_window: str | None = None,
    tool_call_id: str = "",
) -> str:
    return await driver.type_text(text=text, expect_window=expect_window)


async def desktop_scroll(
    session: object,
    driver: DesktopDriver,
    dx: int = 0,
    dy: int = 0,
    x: float | None = None,
    y: float | None = None,
    expect_window: str | None = None,
    tool_call_id: str = "",
) -> str:
    return await driver.scroll(dx=dx, dy=dy, x=x, y=y, expect_window=expect_window)


async def aim_desktop_click(
    driver: DesktopDriver,
    intended_x: float,
    intended_y: float,
    *,
    target: str,
    client: GroundingLocator | None,
) -> tuple[float, float, GroundingResult]:
    """Resolve a click point. Miss / off / down keeps the intended (x, y)."""
    if client is None or not client.enabled:
        return intended_x, intended_y, GroundingResult.fallback(reason="off")

    png, width_points, height_points = await _grounding_screenshot(driver)
    if png is None:
        return intended_x, intended_y, GroundingResult.fallback(reason="capture")

    phrase = target.strip() or (
        f"the control nearest ({intended_x:g}, {intended_y:g}) logical points"
    )
    result = await client.locate(
        png,
        phrase,
        width_points=width_points,
        height_points=height_points,
    )
    if result.x is None or result.y is None:
        return intended_x, intended_y, result
    return result.x, result.y, result


async def _grounding_screenshot(
    driver: DesktopDriver,
) -> tuple[bytes | None, float | None, float | None]:
    """Capture for grounding only — not a persisted screen frame."""
    try:
        with hide_real_display_for_screenshot():
            raw = await driver.screenshot()
    except DesktopError:
        # Capture failure must not fail the click; TD-3304's intended point wins.
        return None, None, None
    png = _png_from_driver_json(raw)
    if png is None:
        return None, None, None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return png, None, None
    if not isinstance(body, dict):
        return png, None, None
    width = _optional_float(body.get("width"))
    height = _optional_float(body.get("height"))
    return png, width, height


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _with_grounding(raw: str, result: GroundingResult) -> str:
    payload: dict[str, object]
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw}
    else:
        payload = loaded if isinstance(loaded, dict) else {"raw": raw}
    payload["grounding"] = {
        "source": result.source,
        "latency_ms": result.latency_ms,
        "cost": result.cost,
        "reason": result.reason,
    }
    return json.dumps(payload)


def register_desktop_handlers(
    dispatcher: ToolDispatcher,
    driver: DesktopDriver,
    grounding_client: GroundingLocator | None = None,
) -> None:
    """Bind the five desktop tools to *driver*."""
    dispatcher.register_handler("desktop_screenshot", partial(desktop_screenshot, driver=driver))
    dispatcher.register_handler("desktop_move", partial(desktop_move, driver=driver))
    dispatcher.register_handler(
        "desktop_click",
        partial(desktop_click, driver=driver, grounding_client=grounding_client),
    )
    dispatcher.register_handler("desktop_type", partial(desktop_type, driver=driver))
    dispatcher.register_handler("desktop_scroll", partial(desktop_scroll, driver=driver))
