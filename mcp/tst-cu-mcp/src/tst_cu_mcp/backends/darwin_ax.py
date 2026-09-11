"""macOS Accessibility-tree walk and actions.

AXUIElement reads and AXPress/AXSetValue do not move the pointer, which is
what makes background computer-use possible. Checkout Python is the wrong
TCC identity, so when this interpreter is not the host it asks the TST Desk
host over ``cu-agent.sock`` (same seam as click/capture). Function-local
pyobjc so the module imports on Windows.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from tst_cu_mcp.tree import flatten_tree

_MAX_WALK_CHILDREN = 200
_AX_TIMEOUT_S = 8.0


class AxUnsupported(RuntimeError):
    """Background UI tools are not available on this platform or host."""


def snapshot_pid(pid: int, *, max_nodes: int = 250, query: str = "") -> dict[str, Any]:
    """Accessibility tree for *pid*, flattened to path-addressed elements."""
    if pid <= 0:
        raise ValueError("pid must be a positive process id")
    payload = _via_host({"op": "ui_snapshot", "pid": pid, "max_nodes": max_nodes, "query": query})
    if payload is not None:
        tree = payload.get("tree")
        if isinstance(tree, dict):
            elements, truncated = flatten_tree(tree, max_nodes=max_nodes, query=query)
            return {
                "pid": pid,
                "elements": elements,
                "truncated": truncated,
                "path": "host",
            }
        return payload
    if sys.platform != "darwin":
        raise AxUnsupported("ui_snapshot is macOS-only; use screenshot and click")
    root = _read_tree(pid)
    elements, truncated = flatten_tree(root, max_nodes=max_nodes, query=query)
    return {"pid": pid, "elements": elements, "truncated": truncated, "path": "local"}


def act_pid(
    pid: int,
    element_id: str,
    action: str,
    value: str | None = None,
) -> dict[str, Any]:
    """Perform *action* on *element_id* inside *pid*. Does not move the pointer."""
    if pid <= 0:
        raise ValueError("pid must be a positive process id")
    verb = action.strip().casefold()
    payload = _via_host(
        {
            "op": "ui_act",
            "pid": pid,
            "id": element_id,
            "action": verb,
            "value": value or "",
        }
    )
    if payload is not None:
        return payload
    if sys.platform != "darwin":
        raise AxUnsupported("ui_action is macOS-only; use screenshot and click")
    return _local_act(pid, element_id, verb, value or "")


def _via_host(body: dict[str, Any]) -> dict[str, Any] | None:
    """Send a JSON command to the host actuator when we are not that process."""
    from tst_cu_mcp.backends.darwin import _cu_transact, _is_host_identity

    if _is_host_identity():
        return None
    raw = _cu_transact("json " + json.dumps(body, separators=(",", ":")), timeout=_AX_TIMEOUT_S)
    if not raw:
        return None
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("ok") is False:
        raise RuntimeError(str(parsed.get("error") or "host ui command failed"))
    return parsed


def _read_tree(pid: int) -> dict[str, Any]:
    app = _application(pid)
    windows = _attr_list(app, "AXWindows")
    children = [_read_element(window) for window in windows[:_MAX_WALK_CHILDREN]]
    return {
        "role": "AXApplication",
        "title": _attr_str(app, "AXTitle"),
        "value": "",
        "description": "",
        "enabled": True,
        "focused": False,
        "actions": ["raise"],
        "children": children,
    }


def _read_element(element: Any) -> dict[str, Any]:
    actions = _action_names(element)
    children_raw = _attr_list(element, "AXChildren")
    children = [_read_element(child) for child in children_raw[:_MAX_WALK_CHILDREN]]
    return {
        "role": _attr_str(element, "AXRole"),
        "title": _attr_str(element, "AXTitle"),
        "value": _attr_str(element, "AXValue"),
        "description": _attr_str(element, "AXDescription") or _attr_str(element, "AXHelp"),
        "enabled": _attr_bool(element, "AXEnabled", default=True),
        "focused": _attr_bool(element, "AXFocused", default=False),
        "actions": actions,
        "children": children,
    }


def _local_act(pid: int, element_id: str, action: str, value: str) -> dict[str, Any]:
    target = _element_at(pid, element_id)
    if action == "press":
        _perform(target, "AXPress")
    elif action == "raise":
        _perform(target, "AXRaise")
    elif action == "show_menu":
        _perform(target, "AXShowMenu")
    elif action == "focus":
        _set_bool(target, "AXFocused", True)
    elif action == "set_value":
        _set_string(target, "AXValue", value)
    else:
        raise ValueError(
            f"unknown ui action {action!r}; use one of press, set_value, focus, raise, show_menu"
        )
    return {"ok": True, "pid": pid, "id": element_id, "action": action, "path": "local"}


def _application(pid: int) -> Any:
    try:
        from ApplicationServices import AXUIElementCreateApplication
    except (ImportError, AttributeError) as exc:
        raise AxUnsupported("ApplicationServices is not available") from exc
    element = AXUIElementCreateApplication(int(pid))
    if element is None:
        raise RuntimeError(f"no accessibility element for pid {pid}")
    return element


def _element_at(pid: int, element_id: str) -> Any:
    raw = element_id.strip()
    if not raw:
        raise KeyError("empty element id")
    app = _application(pid)
    windows = _attr_list(app, "AXWindows")
    node: Any = app
    parts = raw.split(".")
    for index, part in enumerate(parts):
        if not part.isdigit():
            raise KeyError(f"malformed element id {element_id!r}")
        siblings = windows if index == 0 else _attr_list(node, "AXChildren")
        pos = int(part)
        if pos < 0 or pos >= len(siblings):
            raise KeyError(f"element {element_id!r} is not in the tree")
        node = siblings[pos]
    return node


def _attr(element: Any, name: str) -> Any:
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except (ImportError, AttributeError):
        return None
    try:
        result = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    if result is None:
        return None
    if isinstance(result, tuple):
        err, value = result[0], result[1] if len(result) > 1 else None
        if err not in (0, None):
            return None
        return value
    return result


def _attr_str(element: Any, name: str) -> str:
    value = _attr(element, name)
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _attr_bool(element: Any, name: str, *, default: bool) -> bool:
    value = _attr(element, name)
    if value is None:
        return default
    return bool(value)


def _attr_list(element: Any, name: str) -> list[Any]:
    value = _attr(element, name)
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _action_names(element: Any) -> list[str]:
    try:
        from ApplicationServices import AXUIElementCopyActionNames
    except (ImportError, AttributeError):
        return []
    try:
        result = AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    raw: Any = result
    if isinstance(result, tuple):
        err, raw = result[0], result[1] if len(result) > 1 else None
        if err not in (0, None):
            return []
    if raw is None:
        return []
    names = []
    try:
        tokens = list(raw)
    except TypeError:
        return []
    mapping = {
        "AXPress": "press",
        "AXConfirm": "press",
        "AXRaise": "raise",
        "AXShowMenu": "show_menu",
    }
    seen: set[str] = set()
    for token in tokens:
        mapped = mapping.get(str(token))
        if mapped and mapped not in seen:
            seen.add(mapped)
            names.append(mapped)
    if _attr(element, "AXValue") is not None and "set_value" not in seen:
        names.append("set_value")
    names.append("focus")
    return names


def _perform(element: Any, action: str) -> None:
    try:
        from ApplicationServices import AXUIElementPerformAction
    except (ImportError, AttributeError) as exc:
        raise AxUnsupported("ApplicationServices is not available") from exc
    try:
        err = AXUIElementPerformAction(element, action)
    except Exception as exc:
        raise RuntimeError(f"AX {action} failed: {exc}") from exc
    if isinstance(err, tuple):
        err = err[0]
    if err not in (0, None):
        raise RuntimeError(f"AX {action} refused (error {err})")


def _set_string(element: Any, name: str, value: str) -> None:
    try:
        from ApplicationServices import AXUIElementSetAttributeValue
    except (ImportError, AttributeError) as exc:
        raise AxUnsupported("ApplicationServices is not available") from exc
    try:
        err = AXUIElementSetAttributeValue(element, name, value)
    except Exception as exc:
        raise RuntimeError(f"AX set {name} failed: {exc}") from exc
    if isinstance(err, tuple):
        err = err[0]
    if err not in (0, None):
        raise RuntimeError(f"AX set {name} refused (error {err})")


def _set_bool(element: Any, name: str, value: bool) -> None:
    try:
        from ApplicationServices import AXUIElementSetAttributeValue
    except (ImportError, AttributeError) as exc:
        raise AxUnsupported("ApplicationServices is not available") from exc
    try:
        err = AXUIElementSetAttributeValue(element, name, bool(value))
    except Exception as exc:
        raise RuntimeError(f"AX set {name} failed: {exc}") from exc
    if isinstance(err, tuple):
        err = err[0]
    if err not in (0, None):
        raise RuntimeError(f"AX set {name} refused (error {err})")
