"""MCP registration for background (app-scoped) computer-use tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from tst_cu_mcp import ui


def register_background_tools(server: MCPServer) -> None:
    """Add list_apps / ui_snapshot / ui_action / launch / hide / unhide."""

    @server.tool(
        name="list_apps",
        description=(
            "List running user-facing apps (name, pid, bundle id, window titles). "
            "Denied apps are omitted. Use this before ui_snapshot. Does not take "
            "the pointer."
        ),
        structured_output=False,
    )
    def list_apps() -> dict[str, Any]:
        return ui.list_apps()

    @server.tool(
        name="ui_snapshot",
        description=(
            "Read an app's accessibility tree without taking the pointer or "
            "bringing the app forward. Pass `app` as a name, bundle id, or pid "
            "from list_apps. Optional `query` keeps elements whose role, title, "
            "value, or description contain that substring. Each element has an "
            "`id` (path like '0.2.1') for ui_action. Prefer this over screenshot "
            "when driving an allowed app in the background."
        ),
        structured_output=False,
    )
    def ui_snapshot(
        app: str,
        max_nodes: int = ui.DEFAULT_NODES,
        query: str = "",
    ) -> dict[str, Any]:
        return ui.ui_snapshot(app, max_nodes=max_nodes, query=query)

    @server.tool(
        name="ui_action",
        description=(
            "Act on one accessibility element without moving the pointer or "
            "keyboard. `action` is press, set_value, focus, raise, or show_menu. "
            "`element_id` comes from ui_snapshot. `set_value` needs `value`. "
            "The app does not have to be in front. Refused for denied apps."
        ),
        structured_output=False,
    )
    def ui_action(
        app: str,
        element_id: str,
        action: str,
        value: str | None = None,
    ) -> dict[str, Any]:
        return ui.ui_action(app, element_id, action, value)

    @server.tool(
        name="launch_app",
        description=(
            "Open an application by display name or bundle id (macOS `open -a` / "
            "`open -b`) without Spotlight and without taking the pointer. Then "
            "ui_snapshot. Denied apps are refused."
        ),
        structured_output=False,
    )
    def launch_app(app: str) -> dict[str, Any]:
        return ui.launch_app(app)

    @server.tool(
        name="hide_other_apps",
        description=(
            "Full control: hide other regular apps so only `app` (and TST Desk) "
            "stay visible. Background mode should not need this. Pair with "
            "unhide_apps when the task ends."
        ),
        structured_output=False,
    )
    def hide_other_apps(app: str) -> dict[str, Any]:
        return ui.hide_other_apps(app)

    @server.tool(
        name="unhide_apps",
        description=(
            "Restore apps hidden by hide_other_apps in this computer-use process. "
            "Call when a full-control task finishes."
        ),
        structured_output=False,
    )
    def unhide_apps() -> dict[str, Any]:
        return ui.unhide_apps()
