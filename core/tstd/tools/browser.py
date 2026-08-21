"""Browser computer-use tools (TD-1710).

Six verbs through the existing approval gate. Path/host fields stay empty
so PathGuard never runs. Classification comes from ``Tool.actuates`` —
screenshot is Class A; the rest are Class B (ask).
"""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING

from ..browser import BrowserDriver, BrowserError
from ..screen.frames import persist_screen_frame
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

if TYPE_CHECKING:
    from .dispatch import ToolDispatcher


def register_browser_tools(registry: ToolRegistry) -> None:
    """Add the six browser tools. Call from ``create_registry``."""
    registry.register(
        Tool(
            name="browser_screenshot",
            description=(
                "Capture the current browser page as a PNG written to the "
                "session directory. This cannot click or type. Returns the "
                "frame path, not image bytes."
            ),
            parameters={"type": "object", "properties": {}},
            side_effect_class="auto",
            parallel_safe=True,
            mutates=False,
            actuates=False,
        )
    )
    registry.register(
        Tool(
            name="browser_navigate",
            description="Open a URL in the session browser.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s) or about: URL to open"},
                },
                "required": ["url"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="browser_click",
            description="Click a point on the current browser page (CSS pixels).",
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
            name="browser_type",
            description=(
                "Type Unicode at the current keyboard focus in the browser. "
                "Does not move focus — click a field first. The typed text "
                "is never logged."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
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
            name="browser_scroll",
            description="Scroll the browser page. dy>0 scrolls down; dx is horizontal.",
            parameters={
                "type": "object",
                "properties": {
                    "dx": {"type": "integer", "description": "Horizontal scroll", "default": 0},
                    "dy": {"type": "integer", "description": "Vertical scroll", "default": 0},
                },
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="browser_wait",
            description=(
                "Wait for the page. Optional CSS selector; otherwise wait "
                "timeout_ms (default 1000)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "timeout_ms": {
                        "type": "integer",
                        "description": "How long to wait in milliseconds",
                        "default": 1000,
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to wait for",
                    },
                },
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )


def _refuse(exc: BrowserError) -> HandlerRefusal:
    return HandlerRefusal(exc.code, exc.message)


async def _refresh_frame(session: object, driver: BrowserDriver, tool_call_id: str) -> None:
    """Best-effort screenshot after a successful actuation. Never fails the tool."""
    try:
        png = await driver.screenshot_png()
        await persist_screen_frame(session, png, tool_call_id=tool_call_id)
    except (BrowserError, OSError):
        return


async def browser_screenshot(
    session: object,
    driver: BrowserDriver,
    tool_call_id: str = "",
) -> str:
    try:
        png = await driver.screenshot_png()
    except BrowserError as exc:
        raise _refuse(exc) from exc
    body = await persist_screen_frame(session, png, tool_call_id=tool_call_id or None)
    return json.dumps(body)


async def browser_navigate(
    session: object,
    url: str,
    driver: BrowserDriver,
    tool_call_id: str = "",
) -> str:
    try:
        result = await driver.navigate(url)
    except BrowserError as exc:
        raise _refuse(exc) from exc
    await _refresh_frame(session, driver, tool_call_id)
    return json.dumps(result)


async def browser_click(
    session: object,
    x: float,
    y: float,
    driver: BrowserDriver,
    button: str = "left",
    tool_call_id: str = "",
) -> str:
    try:
        result = await driver.click(x=x, y=y, button=button)
    except BrowserError as exc:
        raise _refuse(exc) from exc
    await _refresh_frame(session, driver, tool_call_id)
    return json.dumps(result)


async def browser_type(
    session: object,
    text: str,
    driver: BrowserDriver,
    tool_call_id: str = "",
) -> str:
    try:
        result = await driver.type_text(text)
    except BrowserError as exc:
        raise _refuse(exc) from exc
    await _refresh_frame(session, driver, tool_call_id)
    return json.dumps(result)


async def browser_scroll(
    session: object,
    driver: BrowserDriver,
    dx: int = 0,
    dy: int = 0,
    tool_call_id: str = "",
) -> str:
    try:
        result = await driver.scroll(dx=dx, dy=dy)
    except BrowserError as exc:
        raise _refuse(exc) from exc
    await _refresh_frame(session, driver, tool_call_id)
    return json.dumps(result)


async def browser_wait(
    session: object,
    driver: BrowserDriver,
    timeout_ms: int = 1000,
    selector: str | None = None,
    tool_call_id: str = "",
) -> str:
    try:
        result = await driver.wait(timeout_ms=timeout_ms, selector=selector)
    except BrowserError as exc:
        raise _refuse(exc) from exc
    await _refresh_frame(session, driver, tool_call_id)
    return json.dumps(result)


def register_browser_handlers(dispatcher: ToolDispatcher, driver: BrowserDriver) -> None:
    """Bind the six browser tools to *driver*."""
    dispatcher.register_handler("browser_screenshot", partial(browser_screenshot, driver=driver))
    dispatcher.register_handler("browser_navigate", partial(browser_navigate, driver=driver))
    dispatcher.register_handler("browser_click", partial(browser_click, driver=driver))
    dispatcher.register_handler("browser_type", partial(browser_type, driver=driver))
    dispatcher.register_handler("browser_scroll", partial(browser_scroll, driver=driver))
    dispatcher.register_handler("browser_wait", partial(browser_wait, driver=driver))
