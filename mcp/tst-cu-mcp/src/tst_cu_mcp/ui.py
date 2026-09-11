"""Background computer-use: app-scoped UI without taking the pointer.

Public functions here are what the MCP tools call. Order matches
``input_control``: kill-switch, argument checks, app access policy, then
the platform. Snapshot and list_apps are reads (the kill-switch must not
blind the eyes). Launch, hide, unhide, and ui_action actuate.
"""

from __future__ import annotations

import atexit
from typing import Any

from tst_cu_mcp import safety
from tst_cu_mcp.apps import (
    AppInfo,
    AppNotFoundError,
    assert_access,
    first_match,
    visible_apps,
)
from tst_cu_mcp.backends.darwin_ax import act_pid, snapshot_pid
from tst_cu_mcp.config import Config, load_config, policy_path
from tst_cu_mcp.overlay import get_overlay
from tst_cu_mcp.running import hide_except, launch_named, list_running, unhide_hidden
from tst_cu_mcp.tree import SUPPORTED_ACTIONS

MAX_NODES = 400
DEFAULT_NODES = 250


def policy_config() -> Config:
    """Installed config, or the on-disk file Settings writes if it exists."""
    path = policy_path()
    if path.exists():
        return load_config(path)
    return safety.active_config()


def _policy() -> tuple[tuple[str, ...], tuple[str, ...]]:
    cfg = policy_config()
    return cfg.allowed_apps, cfg.denied_apps


def resolve_app(needle: str) -> AppInfo:
    """The running app *needle* names, after allow/deny."""
    allowed, denied = _policy()
    apps = list_running()
    found = first_match(needle, apps)
    if found is None:
        raise AppNotFoundError(
            f"no running app matches {needle!r}. Call list_apps, or launch_app "
            "first. Background UI tools target an application, not a point."
        )
    assert_access(found, allowed=allowed, denied=denied)
    return found


def list_apps() -> dict[str, Any]:
    """Running user-facing apps the policy lets the model see."""
    allowed, denied = _policy()
    apps = visible_apps(list_running(), allowed=allowed, denied=denied)
    return {
        "apps": [app.to_dict() for app in apps],
        "mode": policy_config().mode,
    }


def ui_snapshot(app: str, max_nodes: int = DEFAULT_NODES, query: str = "") -> dict[str, Any]:
    """Accessibility tree for *app*. Does not take the pointer."""
    if max_nodes < 1 or max_nodes > MAX_NODES:
        raise ValueError(f"max_nodes must be 1..{MAX_NODES}")
    target = resolve_app(app)
    body = snapshot_pid(target.pid, max_nodes=max_nodes, query=query)
    body["app"] = target.to_dict()
    return body


def ui_action(
    app: str,
    element_id: str,
    action: str,
    value: str | None = None,
) -> dict[str, Any]:
    """Press / set_value / focus / raise on an element. No pointer motion."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    verb = action.strip().casefold()
    if verb not in SUPPORTED_ACTIONS:
        raise ValueError(f"unknown ui action {action!r}; use one of {SUPPORTED_ACTIONS}")
    if not element_id.strip():
        raise ValueError("element_id is empty; pass an id from ui_snapshot")
    target = resolve_app(app)
    try:
        result = act_pid(target.pid, element_id.strip(), verb, value)
    except Exception:
        safety.notify_blocked()
        raise
    result["app"] = target.to_dict()
    return result


def launch_app(app: str) -> dict[str, Any]:
    """Open *app* by name or bundle id. Does not take the pointer."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    allowed, denied = _policy()
    # A denied name must not be launched even if it is not running yet.
    probe = AppInfo(name=app, pid=0, bundle_id=app if "." in app else "")
    assert_access(probe, allowed=allowed, denied=denied)
    try:
        return launch_named(app)
    except Exception:
        safety.notify_blocked()
        raise


def hide_other_apps(app: str) -> dict[str, Any]:
    """Hide other regular apps so only *app* (and TST Desk) stay visible."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    target = resolve_app(app)
    try:
        result = hide_except(target)
    except Exception:
        safety.notify_blocked()
        raise
    if policy_config().unhide_on_finish:
        atexit.register(unhide_hidden)
    return result


def unhide_apps() -> dict[str, Any]:
    """Restore apps hidden by hide_other_apps in this process."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    try:
        return unhide_hidden()
    except Exception:
        safety.notify_blocked()
        raise
