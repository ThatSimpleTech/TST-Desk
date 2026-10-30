"""One Settings → Configuration edit, planned on config.yaml's text (TD-5203).

:func:`plan_set` and :func:`plan_reset` check a key path against the
schema (what may be written here, and how much at once), then splice the
edit into the text with :mod:`tstd.config_yaml_edit`. Each plan also says
what the whole would-be file must parse as: :func:`tstd.config_editor.apply_edit`
compares it with what the new text reads back as, so a splice that touched
a neighbour is refused before anything is written.
"""

from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from .config import FILLED_FROM_SHIPPED, default_config_yaml
from .config_editor_checks import (
    ConfigEditRefused,
    check_value,
    check_writable,
    dotted,
    fields_of,
)
from .config_editor_wire import ConfigNode
from .config_schema import ROOT_MODEL
from .config_yaml_edit import remove_value, set_value, top_level_block
from .config_yaml_emit import PathPart

ROOT = ConfigNode(kind="object", ref=ROOT_MODEL)


@dataclass(frozen=True)
class Plan:
    """The would-be text, and what the whole file must read back as."""

    text: str
    expect: dict[str, Any]


@dataclass(frozen=True)
class _Target:
    node: ConfigNode
    #: Each step into a map entry or list item: its place in the path, and the map or list.
    entries: list[tuple[int, ConfigNode]]
    #: The map or list the path ends in an entry of, when it does.
    container: ConfigNode | None


def holds_entries(node: ConfigNode) -> bool:
    """A map or list of groups (models, hooks…): changed one entry at a time."""
    return node.kind in ("map", "list") and any(
        n is not None and n.kind == "object" for n in (node.entries, node.items)
    )


def has(data: Any, path: list[PathPart]) -> bool:
    for part in path:
        in_map = isinstance(data, dict) and isinstance(part, str) and part in data
        in_list = isinstance(data, list) and isinstance(part, int) and 0 <= part < len(data)
        if not (in_map or in_list):
            return False
        data = data[part]
    return True


def get(data: Any, path: list[PathPart]) -> Any:
    for part in path:
        data = data[part]
    return data


def same(a: Any, b: Any) -> bool:
    """Equal as parsed YAML, type for type (``1`` is not ``true``); NaN equals NaN."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return type(a) is type(b) and bool(a == b)


@lru_cache(maxsize=1)
def shipped_raw() -> dict[str, Any]:
    """The packaged config.yaml, parsed."""
    data = yaml.safe_load(default_config_yaml())
    return data if isinstance(data, dict) else {}


def _resolve(path: list[PathPart]) -> _Target:
    node = ROOT
    trail: list[ConfigNode] = []
    entries: list[tuple[int, ConfigNode]] = []
    for i, part in enumerate(path):
        if node.kind == "secrets":
            raise ConfigEditRefused("secret", node.note)
        if node.kind == "object" and isinstance(part, str) and part in fields_of(node):
            node = fields_of(node)[part]
        elif node.kind == "map" and isinstance(part, str) and node.entries is not None:
            entries.append((i, node))
            node = node.entries
        elif node.kind == "list" and isinstance(part, int) and node.items is not None:
            entries.append((i, node))
            node = node.items
        else:
            raise ConfigEditRefused("not_found", f"config.yaml has no key {dotted(path[: i + 1])}")
        trail.append(node)
    for step in trail:
        if step.kind == "secrets":
            raise ConfigEditRefused("secret", step.note)
        if not step.editable:
            raise ConfigEditRefused("not_editable", step.note)
    ends_in_entry = bool(entries) and entries[-1][0] == len(path) - 1
    return _Target(node=node, entries=entries, container=entries[-1][1] if ends_in_entry else None)


def _refuse_whole(target: _Target, path: list[PathPart], verb: str) -> None:
    """A group or a list of groups is never written or reset whole here.

    Written whole, a group would drop the keys the editor never writes
    (an MCP server's keychain references); reset whole, a list of models
    or servers would lose every entry at once.
    """
    node = target.node
    where = dotted(path)
    if holds_entries(node):
        raise ConfigEditRefused("not_editable", f"{where}: add or remove one entry at a time")
    if node.kind == "object" and not node.nullable and target.container is None:
        raise ConfigEditRefused("not_editable", f"{where}: {verb} one key at a time")


def _filled(raw: dict[str, Any], section: PathPart) -> dict[str, Any]:
    """*raw* as the loader sees it: a section it fills from the packaged file, in."""
    if isinstance(section, str) and section not in raw and section in FILLED_FROM_SHIPPED:
        shipped = shipped_raw().get(section)
        if isinstance(shipped, dict | list):
            return {**raw, section: copy.deepcopy(shipped)}
    return raw


def _materialized(text: str, raw: dict[str, Any], section: PathPart) -> str:
    """*text* with a shipped section copied in when the loader fills it from there.

    Writing one key of such a section would otherwise end the fill, and
    every other key in it would fall back to its built-in default.
    """
    if not isinstance(section, str) or section in raw or section not in FILLED_FROM_SHIPPED:
        return text
    block = top_level_block(default_config_yaml(), section)
    if block is None:
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    lead = "" if not text or text.endswith("\n") else nl
    return text + lead + nl + block.replace("\n", nl) + nl


def _with(data: dict[str, Any], path: list[PathPart], value: Any) -> dict[str, Any]:
    """A copy of *data* with *value* at *path*, as :func:`set_value` writes it."""
    out = copy.deepcopy(data)
    holder: Any = out
    for part, after in itertools.pairwise(path):
        child = holder[part] if has(holder, [part]) else None
        if child is None:
            child = [] if isinstance(after, int) else {}
            _put(holder, part, child)
        holder = child
    _put(holder, path[-1], value)
    return out


def _put(holder: Any, part: PathPart, value: Any) -> None:
    if isinstance(holder, list) and part == len(holder):
        holder.append(value)
    else:
        holder[part] = value


def _without(data: dict[str, Any], path: list[PathPart]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    del get(out, path[:-1])[path[-1]]
    return out


def _removal(text: str, raw: dict[str, Any], path: list[PathPart]) -> Plan:
    """The key or entry at *path* removed, from a filled section too.

    A section the loader fills from the packaged file shows entries the
    file does not have; it is copied in first, so the removal lands.
    """
    base = _filled(raw, path[0])
    if not has(base, path):
        return Plan(text, raw)
    filled = _materialized(text, raw, path[0])
    return Plan(remove_value(filled, path), _without(base, path))


def plan_set(text: str, raw: dict[str, Any], path: list[PathPart], value: Any) -> Plan:
    target = _resolve(path)
    node = target.node
    where = dotted(path)
    _refuse_whole(target, path, "change")
    base = _filled(raw, path[0])
    # Every entry the path goes through must exist, or be one this page adds.
    for at, holder in target.entries:
        if not holder.addable and not has(base, path[: at + 1]):
            raise ConfigEditRefused("not_editable", holder.add_note)
    exists = has(base, path)
    # A group is written whole only when it is new (an added entry, a group
    # turned on). Over one that exists it would drop the keys the editor never
    # writes, such as an MCP server's keychain references.
    if node.kind == "object" and value is not None and exists and get(base, path) is not None:
        raise ConfigEditRefused("not_editable", f"{where}: change one key at a time")
    check_value(node, value, where)
    check_writable(value, where)
    if exists and same(get(base, path), value):
        return Plan(text, raw)
    filled = _materialized(text, raw, path[0])
    new_text = set_value(filled, path, value, flow=node.kind == "json")
    return Plan(new_text, _with(base, path, value))


def plan_reset(text: str, raw: dict[str, Any], path: list[PathPart]) -> Plan:
    target = _resolve(path)
    node = target.node
    _refuse_whole(target, path, "reset")
    if target.container is not None:
        if not target.container.removable:
            raise ConfigEditRefused("not_editable", target.container.remove_note)
        return _removal(text, raw, path)
    if not node.resettable:
        raise ConfigEditRefused("not_editable", f"{dotted(path)} is set by its own control")
    if target.entries:
        if not node.has_default:
            raise ConfigEditRefused(
                "not_editable", f"{dotted(path)} has no default; remove the entry instead"
            )
        return _removal(text, raw, path)
    section = path[0]
    if isinstance(section, str) and section not in raw and section in FILLED_FROM_SHIPPED:
        return Plan(text, raw)  # still the shipped section, so already the shipped value
    if not has(shipped_raw(), path):
        return _removal(text, raw, path)
    shipped = get(shipped_raw(), path)
    if has(raw, path) and same(get(raw, path), shipped):
        return Plan(text, raw)
    new_text = set_value(text, path, shipped, flow=node.kind == "json")
    return Plan(new_text, _with(raw, path, shipped))
