"""Edit one value of a YAML document and leave every other byte alone (TD-5203).

Settings → Configuration writes one key at a time into ``config.yaml``: a
file a person also edits, and whose shipped copy is mostly teaching
comments. A round trip through a YAML library re-emits the whole document,
so this module never does one. PyYAML's composer gives every node's exact
start and end in the text (:mod:`tstd.config_yaml_nodes`), and an edit
replaces, inserts or deletes only the characters of the key it changes.
Comments, blank lines, quoting and flow or block style outside that key
stay byte for byte as they were.

New values are written as JSON, which YAML reads back as the same value:
a string is always double-quoted (so ``on``, ``1.0`` or ``2026-10-09``
stays a string), and a float always has a decimal point (YAML 1.1 reads
``1e-05`` as a string). A multi-line string that replaces a ``|`` or ``>``
block stays a block. Inside a flow collection (``{...}`` or ``[...]``)
everything is written in flow style, since a block line there would not
parse. The caller re-reads the result and checks that the value landed;
this module only edits text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import yaml

from .config_yaml_emit import (
    PathPart,
    YamlEditError,
    block_scalar,
    entry_lines,
    flow_text,
    item_lines,
    nest,
    nonempty_collection,
    quote,
)
from .config_yaml_nodes import (
    Slot,
    colon_after,
    compose,
    content_end,
    eol,
    is_block,
    is_null,
    line_start,
    newline,
    next_line,
    walk,
)

#: A block scalar's header: ``|`` or ``>`` and its chomping or indent marks.
_BLOCK_HEADER = re.compile(r"[|>][0-9+-]*")


def set_value(text: str, path: Sequence[PathPart], value: Any, *, flow: bool = False) -> str:
    """*text* with the value at *path* set to *value*, created if missing.

    An int part one past a list's end appends to it. A missing key is added
    at the end of its mapping, with any missing parents. A replaced block
    value stays block style unless *flow*; JSON-shaped values pass *flow*.
    """
    if not path:
        raise YamlEditError("an edit needs a key")
    root = compose(text)
    if root is None:
        return _append_document(text, path, value, flow)
    if not isinstance(root, yaml.MappingNode):
        raise YamlEditError("config.yaml must be a mapping at the top level")
    slots, stop = walk(root, path)
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
    if is_null(stop) and slots:
        return _replace(text, slots[-1], nest(rest, value), flow)
    raise YamlEditError("that key's parent in config.yaml is not a mapping or list")


def _append_document(text: str, path: Sequence[PathPart], value: Any, flow: bool) -> str:
    """A key in a document that has only comments, or nothing at all."""
    if not isinstance(path[0], str):
        raise YamlEditError("config.yaml must be a mapping at the top level")
    nl = newline(text)
    lines = entry_lines(path[0], nest(path[1:], value), 0, flow)
    lead = "" if not text or text.endswith("\n") else nl
    return text + lead + nl.join(lines) + nl


def _in_flow(slot: Slot | None) -> bool:
    """Whether *slot* sits inside a flow collection, where no block line may go.

    A block collection cannot nest inside a flow one, so the parent says it.
    """
    return slot is not None and bool(slot.parent.flow_style)


def _replace(text: str, slot: Slot, value: Any, flow: bool) -> str:
    node = slot.value
    nl = newline(text)
    block_old = is_block(node)
    # A list or map the person wrote in flow style stays in flow style; an
    # empty ``[]`` or ``{}`` is the only way to write empty, not a choice.
    flow_old = (
        isinstance(node, yaml.MappingNode | yaml.SequenceNode)
        and bool(node.flow_style)
        and bool(node.value)
    )
    as_block = nonempty_collection(value) and not flow and not flow_old and not _in_flow(slot)
    if slot.key is None:
        return _replace_item(text, slot, value, as_block)
    colon = colon_after(text, slot.key)
    end = eol(text, content_end(text, node)) if block_old else int(node.end_mark.index)
    if isinstance(node, yaml.ScalarNode) and block_old and isinstance(value, str):
        kept = _replace_block_scalar(text, slot, value, end)
        if kept is not None:
            return kept
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
    line_end = eol(text, end)
    return text[: colon + 1] + text[end:line_end] + nl + nl.join(lines) + text[line_end:]


def _replace_block_scalar(text: str, slot: Slot, value: str, end: int) -> str | None:
    """A ``|`` or ``>`` value given new text, still a block; None when it cannot be.

    A long prompt someone wrote as a block stays readable in the file. A
    comment after the old header stays on the header line.
    """
    assert slot.key is not None
    lines = block_scalar(value, int(slot.key.start_mark.column) + 2)
    if lines is None:
        return None
    start = int(slot.value.start_mark.index)
    head_end = eol(text, start)
    header = _BLOCK_HEADER.match(text, start, head_end)
    after_header = text[header.end() : head_end] if header else ""
    nl = newline(text)
    return text[:start] + lines[0] + after_header + nl + nl.join(lines[1:]) + text[end:]


def _replace_item(text: str, slot: Slot, value: Any, as_block: bool) -> str:
    """A list item, in place: after its ``- `` in a block list."""
    node = slot.value
    start = int(node.start_mark.index)
    end = eol(text, content_end(text, node)) if is_block(node) else int(node.end_mark.index)
    if slot.parent.flow_style or not as_block:
        return text[:start] + flow_text(value) + text[end:]
    nl = newline(text)
    dash = int(slot.parent.start_mark.column)
    lines = item_lines([value], dash)
    lines[0] = lines[0][dash + 2 :]
    return text[:start] + nl.join(lines) + text[end:]


def _append(text: str, holder: Slot | None, seq: yaml.SequenceNode, value: Any, flow: bool) -> str:
    nl = newline(text)
    if seq.flow_style:
        if seq.value:
            at = content_end(text, seq.value[-1])
            return text[:at] + ", " + flow_text(value) + text[at:]
        if holder is not None and holder.key is not None and not flow and not _in_flow(holder):
            colon = colon_after(text, holder.key)
            indent = int(holder.key.start_mark.column) + 2
            lines = item_lines([value], indent)
            return text[: colon + 1] + nl + nl.join(lines) + text[int(seq.end_mark.index) :]
        start, end = int(seq.start_mark.index), int(seq.end_mark.index)
        return text[:start] + "[" + flow_text(value) + "]" + text[end:]
    at = eol(text, content_end(text, seq))
    lines = item_lines([value], int(seq.start_mark.column))
    return text[:at] + nl + nl.join(lines) + text[at:]


def _insert(
    text: str,
    holder: Slot | None,
    mapping: yaml.MappingNode,
    key: str,
    value: Any,
    flow: bool,
) -> str:
    nl = newline(text)
    if mapping.flow_style:
        if mapping.value:
            at = content_end(text, mapping.value[-1][1])
            return text[:at] + f", {quote(key)}: {flow_text(value)}" + text[at:]
        start, end = int(mapping.start_mark.index), int(mapping.end_mark.index)
        if holder is None or holder.key is None or _in_flow(holder):
            return text[:start] + "{" + f"{quote(key)}: {flow_text(value)}" + "}" + text[end:]
        colon = colon_after(text, holder.key)
        indent = int(holder.key.start_mark.column) + 2
        lines = entry_lines(key, value, indent, flow)
        return text[: colon + 1] + nl + nl.join(lines) + text[end:]
    indent = int(mapping.value[0][0].start_mark.column) if mapping.value else 0
    at = eol(text, content_end(text, mapping))
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
    root = compose(text)
    if root is None:
        return text
    slots, _ = walk(root, path)
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
        start = line_start(text, slot.value.start_mark.index)
        if text[start : slot.value.start_mark.index].strip() != "-":
            raise YamlEditError("this list item shares its line with another; edit it by hand")
    else:
        start = line_start(text, slot.key.start_mark.index)
        if text[start : slot.key.start_mark.index].strip():
            if slot.index + 1 >= len(parent.value):
                raise YamlEditError("this key shares its line with another; edit it by hand")
            return _remove_first_key(text, slot)
    end = next_line(text, eol(text, content_end(text, slot.value)))
    return text[:start] + text[end:]


def _remove_first_key(text: str, slot: Slot) -> str:
    """The first key of a block list item goes, and the next key moves up after its ``- ``.

    The comment and blank lines that sat between the two keys move above
    the item, so a note about the next key is not lost with this one.
    """
    assert slot.key is not None
    key_at = int(slot.key.start_mark.index)
    follow_at = int(slot.parent.value[slot.index + 1][0].start_mark.index)
    dash_line = line_start(text, key_at)
    after_value = next_line(text, eol(text, content_end(text, slot.value)))
    between = text[after_value : line_start(text, follow_at)]
    return text[:dash_line] + between + text[dash_line:key_at] + text[follow_at:]


def _remove_flow(text: str, slot: Slot) -> str:
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
    return text[: content_end(text, prev_node)] + text[content_end(text, slot.value) :]


def top_level_block(text: str, key: str) -> str | None:
    """The text of a top-level section, with the comment lines just above it."""
    root = compose(text)
    if not isinstance(root, yaml.MappingNode):
        return None
    slots, _ = walk(root, [key])
    if not slots:
        return None
    slot = slots[0]
    assert slot.key is not None
    start = line_start(text, slot.key.start_mark.index)
    while start > 0:
        above = line_start(text, start - 1)
        if not text[above:start].strip().startswith("#"):
            break
        start = above
    return text[start : eol(text, content_end(text, slot.value))]
