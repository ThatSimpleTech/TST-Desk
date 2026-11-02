"""Element-level desktop tools on the accessibility tree (TD-5220).

Registered only when ``computer_use.accessibility.enabled`` is on. The
``desktop_`` prefix puts them under the same kill-switch, on-screen ring
and first-run permission panel as the coordinate tools.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from ..desktop.ax_driver import ACTIONS, AccessibilityDriver
from .registry import Tool, ToolRegistry

if TYPE_CHECKING:
    from .dispatch import ToolDispatcher

AX_TOOL_NAMES = ("desktop_windows", "desktop_snapshot", "desktop_act", "desktop_launch")


def register_ax_tools(registry: ToolRegistry) -> None:
    """Add the four accessibility tools. Call only when the feature is on."""
    registry.register(
        Tool(
            name="desktop_windows",
            description=(
                "List open desktop windows (window_id, app, title, pid). Read-only. "
                "Use a window_id with desktop_snapshot."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Only this app's windows"},
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
            name="desktop_snapshot",
            description=(
                "Read the interactive elements of one desktop window as text, one per "
                "line with a ref like @s8f3k2p9:e1. Read-only, does not move the pointer. "
                "Pass window_id (from desktop_windows), app, or root (a ref, to open a "
                "folded menu or a cut branch). Refs last until the window changes; after "
                "acting, snapshot again. Prefer this over screenshots for native apps. "
                "Browsers and web views expose little here; use the browser or "
                "coordinate tools there."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "window_id": {"type": "string", "description": "From desktop_windows"},
                    "app": {"type": "string", "description": "App name, if it has one window"},
                    "root": {"type": "string", "description": "A ref to open as the root"},
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
            name="desktop_act",
            description=(
                "Act on one element by its ref from desktop_snapshot, without moving the "
                "pointer: click, type (inserts value at the caret), set_value (replaces "
                "the field), toggle, select (value is the option), expand, scroll_to. "
                "Password fields are refused: ask the user to type those. On "
                "ax_stale_ref or ax_ambiguous, snapshot again and use the new ref."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref from desktop_snapshot"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "value": {
                        "type": "string",
                        "description": "Text for type / set_value, option for select",
                    },
                },
                "required": ["ref", "action"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )
    registry.register(
        Tool(
            name="desktop_launch",
            description=(
                "Start a desktop app by name or path with its accessibility switched on, "
                "and return its window_id. An app that was already running before may "
                "need restarting by the user to become readable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Application name or path"},
                },
                "required": ["app"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
            actuates=True,
        )
    )


async def desktop_windows(
    session: object, driver: AccessibilityDriver, app: str | None = None, tool_call_id: str = ""
) -> str:
    return await driver.windows(app=app)


async def desktop_snapshot(
    session: object,
    driver: AccessibilityDriver,
    window_id: str | None = None,
    app: str | None = None,
    root: str | None = None,
    tool_call_id: str = "",
) -> str:
    return await driver.snapshot(app=app, window_id=window_id, root=root)


async def desktop_act(
    session: object,
    ref: str,
    action: str,
    driver: AccessibilityDriver,
    value: str | None = None,
    tool_call_id: str = "",
) -> str:
    return await driver.act(ref=ref, action=action, value=value)


async def desktop_launch(
    session: object, app: str, driver: AccessibilityDriver, tool_call_id: str = ""
) -> str:
    return await driver.launch(app)


def register_ax_handlers(dispatcher: ToolDispatcher, driver: AccessibilityDriver) -> None:
    """Bind the four accessibility tools to *driver*."""
    dispatcher.register_handler("desktop_windows", partial(desktop_windows, driver=driver))
    dispatcher.register_handler("desktop_snapshot", partial(desktop_snapshot, driver=driver))
    dispatcher.register_handler("desktop_act", partial(desktop_act, driver=driver))
    dispatcher.register_handler("desktop_launch", partial(desktop_launch, driver=driver))
