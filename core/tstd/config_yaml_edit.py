"""Edit one value of a YAML document and leave every other byte alone (TD-5203).

Settings → Configuration writes one key at a time into ``config.yaml``: a
file a person also edits, and whose shipped copy is mostly teaching
comments. A round trip through a YAML library re-emits the whole document,
so this module never does one. PyYAML's composer gives every node's exact
start and end in the text, and an edit replaces, inserts or deletes only
the characters of the key it changes. Comments, blank lines, quoting and
flow or block style outside that key stay byte for byte as they were.

New values are written as JSON, which YAML reads back as the same value:
a string is always double-quoted (so ``on``, ``1.0`` or ``2026-10-09``
stays a string), and a float always has a decimal point (YAML 1.1 reads
``1e-05`` as a string). The caller re-reads the result and checks that the
value landed; this module only edits text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from .config_yaml_emit import (
    PathPart,
    YamlEditError,
    entry_lines,
    flow_text,
    item_lines,
    nest,
    nonempty_collection,
    quote,
)

_NULL_TAG = "tag:yaml.org,2002:null"
_STR_TAG = "tag:yaml.org,2002:str"


@dataclass(frozen=True)
class _Slot:
    """Where one value sits: its parent collection, position and key."""

    parent: yaml.nodes.CollectionNode
    index: int
    key: yaml.ScalarNode | None
    value: yaml.Node


# ── reading positions ────────────────────────────────────────────────


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _eol(text: str, index: int) -> int:
    """Where the line holding *index* ends, before its line break."""
    found = text.find("\n", index)
    if found < 0:
        return len(text)
    return found - 1 if found > 0 and text[found - 1] == "\r" else found


def _next_line(text: str, eol: int) -> int:
    if eol >= len(text):
        return len(text)
    return eol + 2 if text.startswith("\r\n", eol) else eol + 1


def _is_block(node: yaml.Node) -> bool:
    if isinstance(node, yaml.ScalarNode):
        return node.style in ("|", ">")
    return isinstance(node, yaml.nodes.CollectionNode) and not node.flow_style


def _content_end(text: str, node: yaml.Node) -> int:
    """One past the last character that belongs to *node*.

    A block collection's own end mark runs on over the comments and blank
    lines that follow it, up to the next token, so it is taken from its
    last child instead. A block scalar's runs over its trailing blank lines.
    """
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark.index
        if node.style in ("|", ">"):
            while end > node.start_mark.index and text[end - 1] in " \t\r\n":
                end -= 1
        return int(end)
    assert isinstance(node, yaml.nodes.CollectionNode)
    if node.flow_style or not node.value:
        return int(node.end_mark.index)
    last = node.value[-1]
    if isinstance(node, yaml.MappingNode):
        key, value = last
        return max(_content_end(text, value), int(key.end_mark.index))
    return _content_end(text, last)


def _colon_after(text: str, key: yaml.ScalarNode) -> int:
    at = text.find(":", key.end_mark.index)
    if at < 0:
        raise YamlEditError("a mapping key has no colon")
    return at


def _key_of(node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG:
        return node.value
    return yaml.SafeLoader("").construct_object(node, deep=True)


def _shared(root: yaml.Node) -> set[int]:
    """Nodes reachable twice: the target of an alias, and so of an anchor."""
    seen: set[int] = set()
    shared: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            shared.add(id(node))
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return shared


def _compose(text: str) -> yaml.Node | None:
    try:
        node: yaml.Node | None = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        raise YamlEditError(f"config.yaml is not valid YAML: {e}") from e
    return node


def _walk(root: yaml.Node, path: Sequence[PathPart]) -> tuple[list[_Slot], yaml.Node]:
    """The slots along *path* that exist, and the node the walk stopped in."""
    shared = _shared(root)
    slots: list[_Slot] = []
    node = root
    for part in path:
        if id(node) in shared:
            raise YamlEditError(
                "this key is inside a YAML anchor or alias in config.yaml; edit it by hand"
            )
        slot: _Slot | None = None
        if isinstance(node, yaml.MappingNode) and isinstance(part, str):
            for i, (key, value) in enumerate(node.value):
                if _key_of(key) == part:
                    slot = _Slot(node, i, key, value)
                    break
        elif (
            isinstance(node, yaml.SequenceNode)
            and isinstance(part, int)
            and 0 <= part < len(node.value)
        ):
            slot = _Slot(node, part, None, node.value[part])
        if slot is None:
            break
        slots.append(slot)
        node = slot.value
    if len(slots) == len(path) and id(node) in shared:
        raise YamlEditError("this key is a YAML anchor or alias in config.yaml; edit it by hand")
    return slots, node


def _is_null(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.tag == _NULL_TAG


# ── edits ────────────────────────────────────────────────────────────


def set_value(text: str, path: Sequence[PathPart], value: Any, *, flow: bool = False) -> str:
    """*text* with the value at *path* set to *value*, created if missing.

    An int part one past a list's end appends to it. A missing key is added
    at the end of its mapping, with any missing parents. A replaced block
    value stays block style unless *flow*; JSON-shaped values pass *flow*.
    """
    if not path:
        raise YamlEditError("an edit needs a key")
    root = _compose(text)
    if root is None:
        return _append_document(text, path, value, flow)
    if not isinstance(root, yaml.MappingNode):
        raise YamlEditError("config.yaml must be a mapping at the top level")
    slots, stop = _walk(root, path)
    if len(slots) == len(path):
        return _replace(text, slots[-1], value, flow)
    done = len(slots)
    rest = path[done:]
    if isinstance(stop, yaml.SequenceNode) and isinstance(rest[0], int):
        if rest[0] != len(stop.value):
            raise YamlEditError(f"list item {rest[0]} does not exist")
        holder = slots[-1] if slots else None
        return _append(text, holder, stop, nest(rest[1:], value), flow)
    if isinstance(stop, yaml.MappingNode) and isinstance(rest[0], str):
        holder = slots[-1] if slots else None
        return _insert(text, holder, stop, rest[0], nest(rest[1:], value), flow)
    if _is_null(stop) and slots:
        return _replace(text, slots[-1], nest(rest, value), flow)
    raise YamlEditError("that key's parent in config.yaml is not a mapping or list")


def _append_document(text: str, path: Sequence[PathPart], value: Any, flow: bool) -> str:
    """A key in a document that has only comments, or nothing at all."""
    if not isinstance(path[0], str):
        raise YamlEditError("config.yaml must be a mapping at the top level")
    nl = _newline(text)
    lines = entry_lines(path[0], nest(path[1:], value), 0, flow)
    lead = "" if not text or text.endswith("\n") else nl
    return text + lead + nl.join(lines) + nl


def _replace(text: str, slot: _Slot, value: Any, flow: bool) -> str:
    node = slot.value
    nl = _newline(text)
    block_old = _is_block(node)
    # A list or map the person wrote in flow style stays in flow style; an
    # empty ``[]`` or ``{}`` is the only way to write empty, not a choice.
    flow_old = (
        isinstance(node, yaml.MappingNode | yaml.SequenceNode)
        and bool(node.flow_style)
        and bool(node.value)
    )
    as_block = nonempty_collection(value) and not flow and not flow_old
    if slot.key is None:
        return _replace_item(text, slot, value, as_block)
    colon = _colon_after(text, slot.key)
    end = _eol(text, _content_end(text, node)) if block_old else int(node.end_mark.index)
    if not as_block:
        return text[: colon + 1] + " " + flow_text(value) + text[end:]
    if isinstance(node, yaml.MappingNode | yaml.SequenceNode) and block_old:
        indent = int(node.start_mark.column)
    else:
        indent = int(slot.key.start_mark.column) + 2
    if isinstance(value, dict):
        lines = [ln for k, v in value.items() for ln in entry_lines(str(k), v, indent, False)]
    else:
        lines = item_lines(value, indent)
    if block_old:
        return text[: colon + 1] + nl + nl.join(lines) + text[end:]
    # Keep a comment that trailed the old inline value on the key's line.
    line_end = _eol(text, end)
    return text[: colon + 1] + text[end:line_end] + nl + nl.join(lines) + text[line_end:]


def _replace_item(text: str, slot: _Slot, value: Any, as_block: bool) -> str:
    """A list item, in place: after its ``- `` in a block list."""
    node = slot.value
    start = int(node.start_mark.index)
    end = _eol(text, _content_end(text, node)) if _is_block(node) else int(node.end_mark.index)
    if slot.parent.flow_style or not as_block:
        return text[:start] + flow_text(value) + text[end:]
    nl = _newline(text)
    dash = int(slot.parent.start_mark.column)
    lines = item_lines([value], dash)
    lines[0] = lines[0][dash + 2 :]
    return text[:start] + nl.join(lines) + text[end:]


def _append(text: str, holder: _Slot | None, seq: yaml.SequenceNode, value: Any, flow: bool) -> str:
    nl = _newline(text)
    if seq.flow_style:
        if seq.value:
            at = _content_end(text, seq.value[-1])
            return text[:at] + ", " + flow_text(value) + text[at:]
        if holder is not None and holder.key is not None and not flow:
            colon = _colon_after(text, holder.key)
            indent = int(holder.key.start_mark.column) + 2
            lines = item_lines([value], indent)
            return text[: colon + 1] + nl + nl.join(lines) + text[int(seq.end_mark.index) :]
        start, end = int(seq.start_mark.index), int(seq.end_mark.index)
        return text[:start] + "[" + flow_text(value) + "]" + text[end:]
    at = _eol(text, _content_end(text, seq))
    lines = item_lines([value], int(seq.start_mark.column))
    return text[:at] + nl + nl.join(lines) + text[at:]


def _insert(
    text: str,
    holder: _Slot | None,
    mapping: yaml.MappingNode,
    key: str,
    value: Any,
    flow: bool,
) -> str:
    nl = _newline(text)
    if mapping.flow_style:
        if mapping.value:
            at = _content_end(text, mapping.value[-1][1])
            return text[:at] + f", {quote(key)}: {flow_text(value)}" + text[at:]
        start, end = int(mapping.start_mark.index), int(mapping.end_mark.index)
        if holder is None or holder.key is None:
            return text[:start] + "{" + f"{quote(key)}: {flow_text(value)}" + "}" + text[end:]
        colon = _colon_after(text, holder.key)
        indent = int(holder.key.start_mark.column) + 2
        lines = entry_lines(key, value, indent, flow)
        return text[: colon + 1] + nl + nl.join(lines) + text[end:]
    indent = int(mapping.value[0][0].start_mark.column) if mapping.value else 0
    at = _eol(text, _content_end(text, mapping))
    lines = entry_lines(key, value, indent, flow)
    # A new top-level section gets a blank line before it, like the others.
    lead = nl + nl if holder is None else nl
    return text[:at] + lead + nl.join(lines) + text[at:]


def remove_value(text: str, path: Sequence[PathPart]) -> str:
    """*text* without the key or list item at *path*; unchanged when absent.

    A mapping or list left empty becomes ``{}`` or ``[]``, so the key that
    held it keeps meaning an empty one rather than null.
    """
    if not path:
        raise YamlEditError("an edit needs a key")
    root = _compose(text)
    if root is None:
        return text
    slots, _ = _walk(root, path)
    if len(slots) < len(path):
        return text
    slot = slots[-1]
    parent = slot.parent
    if len(parent.value) == 1:
        if len(slots) < 2:
            raise YamlEditError("config.yaml cannot lose its last top-level key")
        empty: dict[str, Any] | list[Any] = {} if isinstance(parent, yaml.MappingNode) else []
        return _replace(text, slots[-2], empty, True)
    if parent.flow_style:
        return _remove_flow(text, slot)
    if slot.key is None:
        start = _line_start(text, slot.value.start_mark.index)
        if text[start : slot.value.start_mark.index].strip() != "-":
            raise YamlEditError("this list item shares its line with another; edit it by hand")
    else:
        start = _line_start(text, slot.key.start_mark.index)
        if text[start : slot.key.start_mark.index].strip():
            # The first key of a list item, after its "- ": the next key moves up.
            if slot.index + 1 >= len(parent.value):
                raise YamlEditError("this key shares its line with another; edit it by hand")
            following = parent.value[slot.index + 1][0]
            return text[: slot.key.start_mark.index] + text[following.start_mark.index :]
    end = _next_line(text, _eol(text, _content_end(text, slot.value)))
    return text[:start] + text[end:]


def _remove_flow(text: str, slot: _Slot) -> str:
    parent = slot.parent
    nodes = (
        [key for key, _ in parent.value]
        if isinstance(parent, yaml.MappingNode)
        else list(parent.value)
    )
    if slot.index < len(nodes) - 1:
        start = int(nodes[slot.index].start_mark.index)
        return text[:start] + text[int(nodes[slot.index + 1].start_mark.index) :]
    previous = parent.value[slot.index - 1]
    prev_node = previous[1] if isinstance(parent, yaml.MappingNode) else previous
    return text[: _content_end(text, prev_node)] + text[_content_end(text, slot.value) :]


def top_level_block(text: str, key: str) -> str | None:
    """The text of a top-level section, with the comment lines just above it."""
    root = _compose(text)
    if not isinstance(root, yaml.MappingNode):
        return None
    slots, _ = _walk(root, [key])
    if not slots:
        return None
    slot = slots[0]
    assert slot.key is not None
    start = _line_start(text, slot.key.start_mark.index)
    while start > 0:
        above = _line_start(text, start - 1)
        if not text[above:start].strip().startswith("#"):
            break
        start = above
    return text[start : _eol(text, _content_end(text, slot.value))]
