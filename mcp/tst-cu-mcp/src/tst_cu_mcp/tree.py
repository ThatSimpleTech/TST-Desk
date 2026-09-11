"""Pure accessibility-tree flattening.

The Darwin walker (and the host-side port) produce a nested node dict;
this module assigns path ids, drops untitled noise, and applies the
query filter. Kept pure so the policy is tested without a desktop.
"""

from __future__ import annotations

from typing import Any

# Roles that are layout chrome. Walk through them so their children stay
# reachable, but do not emit a node unless they carry a title or description
# the model could aim at.
NOISE_ROLES = frozenset(
    {
        "AXGroup",
        "AXSplitter",
        "AXLayoutArea",
        "AXLayoutItem",
        "AXUnknown",
        "AXScrollBar",
        "AXDisclosureTriangle",
        "AXBusyIndicator",
        "AXValueIndicator",
    }
)

SUPPORTED_ACTIONS = ("press", "set_value", "focus", "raise", "show_menu")


def flatten_tree(
    root: dict[str, Any],
    *,
    max_nodes: int = 250,
    query: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Flatten *root* into path-addressed elements.

    Ids are child-index paths from the application: ``0`` is the first
    window, ``0.2.1`` is that window's third child's second child. The
    walk stops at *max_nodes* emitted elements (noise that is skipped
    does not count). *query* is a case-insensitive substring against
    role, title, value, and description; empty keeps everything emitted.
    """
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")
    acc: list[dict[str, Any]] = []
    truncated = _walk(root, "", None, acc, max_nodes, query.strip().casefold())
    return acc, truncated


def _walk(
    node: dict[str, Any],
    path: str,
    parent: str | None,
    acc: list[dict[str, Any]],
    max_nodes: int,
    query: str,
) -> bool:
    if len(acc) >= max_nodes:
        return True
    role = str(node.get("role") or "")
    title = str(node.get("title") or "")
    value = str(node.get("value") or "")
    description = str(node.get("description") or "")
    noisy = role in NOISE_ROLES and not title and not description
    emitted_id: str | None = None
    if path and not noisy and (not query or _matches(query, role, title, value, description)):
        emitted_id = path
        acc.append(
            {
                "id": path,
                "parent": parent,
                "role": role,
                "title": title,
                "value": value,
                "description": description,
                "enabled": bool(node.get("enabled", True)),
                "focused": bool(node.get("focused", False)),
                "actions": _normalize_actions(node.get("actions")),
            }
        )
        if len(acc) >= max_nodes:
            return True
    children = node.get("children") or []
    if not isinstance(children, list):
        return False
    next_parent = emitted_id if emitted_id is not None else parent
    truncated = False
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        child_path = str(index) if not path else f"{path}.{index}"
        if _walk(child, child_path, next_parent, acc, max_nodes, query):
            truncated = True
            break
    return truncated


def _matches(query: str, role: str, title: str, value: str, description: str) -> bool:
    return (
        query in role.casefold()
        or query in title.casefold()
        or query in value.casefold()
        or query in description.casefold()
    )


def _normalize_actions(raw: object) -> list[str]:
    names: list[str] = []
    if isinstance(raw, (list, tuple)):
        tokens = [str(item) for item in raw]
    elif isinstance(raw, str):
        tokens = [raw]
    else:
        tokens = []
    mapping = {
        "axpress": "press",
        "press": "press",
        "axconfirm": "press",
        "axraise": "raise",
        "raise": "raise",
        "axshowmenu": "show_menu",
        "show_menu": "show_menu",
        "axsetvalue": "set_value",
        "set_value": "set_value",
        "focus": "focus",
    }
    seen: set[str] = set()
    for token in tokens:
        mapped = mapping.get(token.strip().casefold())
        if mapped and mapped not in seen:
            seen.add(mapped)
            names.append(mapped)
    # Text-bearing roles can take set_value even when AX does not advertise it.
    return names


def resolve_path(root: dict[str, Any], element_id: str) -> dict[str, Any]:
    """Return the nested node at *element_id*, or raise ``KeyError``."""
    raw = element_id.strip()
    if not raw:
        raise KeyError("empty element id")
    node = root
    for part in raw.split("."):
        if not part.isdigit():
            raise KeyError(f"malformed element id {element_id!r}")
        children = node.get("children") or []
        index = int(part)
        if not isinstance(children, list) or index < 0 or index >= len(children):
            raise KeyError(f"element {element_id!r} is not in the tree")
        child = children[index]
        if not isinstance(child, dict):
            raise KeyError(f"element {element_id!r} is not in the tree")
        node = child
    return node
