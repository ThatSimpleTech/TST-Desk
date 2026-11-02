"""Shape an ``agent-desktop`` snapshot for the model (TD-5220).

Raw accessibility trees are noisy: hidden nameless buttons, whole menu
bars, containers with nothing in them. One text editor's interactive tree
was 46 KB, most of it menu items. This module filters, folds menus,
removes secure values and renders one compact line per node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Roles whose subtree is folded unless it is the snapshot root.
FOLDED_ROLES = frozenset({"menu"})
#: Nameless nodes in these states are noise.
_NOISE_STATES = frozenset({"hidden", "disabled"})
_VALUE_CHARS = 120


@dataclass(frozen=True)
class NodeInfo:
    """What the guards need to know about one ref."""

    role: str
    name: str
    states: tuple[str, ...]

    @property
    def secure(self) -> bool:
        return is_secure(self.role, self.states)


def is_secure(role: str, states: tuple[str, ...] | list[str]) -> bool:
    """A password field: flagged ``secure``, or a password role."""
    return "secure" in states or "password" in role.casefold()


def _states(node: dict[str, Any]) -> tuple[str, ...]:
    raw = node.get("states")
    return tuple(str(s) for s in raw) if isinstance(raw, list) else ()


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    raw = node.get("children")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def count_refs(node: dict[str, Any]) -> int:
    own = 1 if node.get("ref_id") else 0
    return own + sum(count_refs(child) for child in _children(node))


def prune(node: dict[str, Any], *, is_root: bool = True) -> dict[str, Any] | None:
    """Filtered copy of *node*, or ``None`` when nothing in it is worth showing."""
    role = str(node.get("role") or "")
    states = _states(node)
    out = {k: v for k, v in node.items() if k != "children"}
    if is_secure(role, states):
        out.pop("value", None)
    if role in FOLDED_ROLES and not is_root:
        folded = sum(count_refs(child) for child in _children(node))
        if folded:
            out["folded"] = folded
        kept: list[dict[str, Any]] = []
    else:
        kept = [p for c in _children(node) if (p := prune(c, is_root=False)) is not None]
    if kept:
        out["children"] = kept
    if is_root or kept or out.get("folded") or out.get("children_count"):
        return out
    named = bool(str(node.get("name") or "").strip())
    if not named and (set(states) & _NOISE_STATES or not node.get("ref_id")):
        return None
    return out


def index(node: dict[str, Any]) -> dict[str, NodeInfo]:
    """Every ref in *node* (unpruned, so the guard sees folded nodes too)."""
    found: dict[str, NodeInfo] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        ref = current.get("ref_id")
        if isinstance(ref, str) and ref:
            found[ref] = NodeInfo(
                role=str(current.get("role") or ""),
                name=str(current.get("name") or ""),
                states=_states(current),
            )
        stack.extend(_children(current))
    return found


def _line(node: dict[str, Any], depth: int, refs: bool) -> str:
    parts = [str(node.get("role") or "?")]
    name = str(node.get("name") or "").strip()
    if name:
        parts.append(json.dumps(name[:_VALUE_CHARS], ensure_ascii=False))
    ref = node.get("ref_id")
    if ref and refs:
        parts.append(str(ref))
    states = _states(node)
    if states:
        parts.append("[" + ",".join(states) + "]")
    value = node.get("value")
    if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
        parts.append("= " + json.dumps(str(value)[:_VALUE_CHARS], ensure_ascii=False))
    if node.get("folded"):
        parts.append(
            f"({node['folded']} items folded" + (f"; snapshot root={ref} to open)" if refs else ")")
        )
    elif isinstance(node.get("children_count"), int) and not node.get("children"):
        parts.append(
            f"(+{node['children_count']} children"
            + (f"; snapshot root={ref} to open)" if refs else ")")
        )
    return "  " * depth + " ".join(parts)


def render(node: dict[str, Any], depth: int = 0, *, refs: bool = True) -> list[str]:
    """One line per node. ``refs=False`` drops refs, which differ every snapshot."""
    lines = [_line(node, depth, refs)]
    for child in _children(node):
        lines.extend(render(child, depth + 1, refs=refs))
    return lines


def cap(lines: list[str], max_chars: int) -> tuple[str, bool]:
    """Join *lines* within *max_chars*; the flag says something was cut."""
    whole = "\n".join(lines)
    if len(whole) <= max_chars:
        return whole, False
    budget = max_chars - 80  # room for the note below
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(f"… {len(lines) - len(kept)} more lines cut; snapshot a smaller root to see them")
    return "\n".join(kept), True


def state_text(data: dict[str, Any], max_chars: int) -> str | None:
    """Compact window state for action verification (TD-709)."""
    tree = data.get("tree")
    if not isinstance(tree, dict):
        return None
    window = data.get("window")
    title = window.get("title") if isinstance(window, dict) else None
    pruned = prune(tree)
    lines = [f"window {json.dumps(str(title or ''), ensure_ascii=False)}"]
    if pruned is not None:
        lines.extend(render(pruned, refs=False))
    text, _cut = cap(lines, max_chars)
    return text
