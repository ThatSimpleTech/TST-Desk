"""Where each value of a YAML document sits in its text (TD-5203).

:mod:`tstd.config_yaml_edit` changes one value by replacing, inserting or
deleting characters; this module finds which characters. PyYAML's composer
gives every node its start and end in the text, and the walk here follows a
key path down to the node it names. A document the splicer cannot place an
edit in safely (a key behind an anchor or a merge, a key listed twice, a
line break YAML sees and these helpers would not) is refused with
:class:`~tstd.config_yaml_emit.YamlEditError` and a reason, so a person
edits it by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import yaml

from .config_yaml_emit import PathPart, YamlEditError

_NULL_TAG = "tag:yaml.org,2002:null"
_STR_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"
#: Line breaks YAML honours besides \n and \r. A position helper here sees
#: only \n, so a key after one of these on the same physical line would be
#: cut along with its neighbour.
_UNICODE_BREAKS = ("\x85", "\u2028", "\u2029")


class _Unmatched:
    """A key that equals no path part: one the walk cannot read as a value."""


@dataclass(frozen=True)
class Slot:
    """Where one value sits: its parent collection, position and key."""

    parent: yaml.nodes.CollectionNode
    index: int
    key: yaml.ScalarNode | None
    value: yaml.Node


def newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def eol(text: str, index: int) -> int:
    """Where the line holding *index* ends, before its line break."""
    found = text.find("\n", index)
    if found < 0:
        return len(text)
    return found - 1 if found > 0 and text[found - 1] == "\r" else found


def next_line(text: str, end: int) -> int:
    if end >= len(text):
        return len(text)
    return end + 2 if text.startswith("\r\n", end) else end + 1


def is_block(node: yaml.Node) -> bool:
    if isinstance(node, yaml.ScalarNode):
        return node.style in ("|", ">")
    return isinstance(node, yaml.nodes.CollectionNode) and not node.flow_style


def content_end(text: str, node: yaml.Node) -> int:
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
        return max(content_end(text, value), int(key.end_mark.index))
    return content_end(text, last)


def colon_after(text: str, key: yaml.ScalarNode) -> int:
    at = text.find(":", key.end_mark.index)
    if at < 0:
        raise YamlEditError("a mapping key has no colon")
    return at


def is_null(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.tag == _NULL_TAG


def _key_of(node: yaml.Node) -> object:
    """A mapping key as a value, without running YAML's merge or value keys."""
    if isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG:
        return node.value
    try:
        return yaml.SafeLoader("").construct_object(node, deep=True)
    except yaml.YAMLError:
        return _Unmatched()


def shared_nodes(root: yaml.Node) -> set[int]:
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


def compose(text: str) -> yaml.Node | None:
    if any(mark in text for mark in _UNICODE_BREAKS):
        raise YamlEditError(
            "config.yaml contains a Unicode line separator (U+0085, U+2028 or U+2029); "
            "edit it by hand"
        )
    try:
        node: yaml.Node | None = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        raise YamlEditError(f"config.yaml is not valid YAML: {e}") from e
    return node


def _find_key(node: yaml.MappingNode, part: str) -> int | None:
    """The position of *part* among *node*'s keys, or None.

    A merge (``<<``) brings in keys from elsewhere, and of a key listed twice
    the loader keeps the last while an edit would find the first: both are
    refused rather than edited in the wrong place.
    """
    found: int | None = None
    for i, (key, _) in enumerate(node.value):
        if key.tag == _MERGE_TAG:
            raise YamlEditError(
                "this key is in a mapping that uses a YAML merge (<<) in config.yaml; "
                "edit it by hand"
            )
        if _key_of(key) == part:
            if found is not None:
                raise YamlEditError(
                    f"config.yaml lists {part} twice in one place; remove one by hand"
                )
            found = i
    return found


def walk(root: yaml.Node, path: Sequence[PathPart]) -> tuple[list[Slot], yaml.Node]:
    """The slots along *path* that exist, and the node the walk stopped in."""
    shared = shared_nodes(root)
    slots: list[Slot] = []
    node = root
    for part in path:
        if id(node) in shared:
            raise YamlEditError(
                "this key is inside a YAML anchor or alias in config.yaml; edit it by hand"
            )
        slot: Slot | None = None
        if isinstance(node, yaml.MappingNode) and isinstance(part, str):
            at = _find_key(node, part)
            if at is not None:
                key, value = node.value[at]
                slot = Slot(node, at, key, value)
        elif (
            isinstance(node, yaml.SequenceNode)
            and isinstance(part, int)
            and 0 <= part < len(node.value)
        ):
            slot = Slot(node, part, None, node.value[part])
        if slot is None:
            break
        slots.append(slot)
        node = slot.value
    if len(slots) == len(path) and id(node) in shared:
        raise YamlEditError("this key is a YAML anchor or alias in config.yaml; edit it by hand")
    return slots, node


def anchored_paths(root: yaml.Node) -> list[list[PathPart]]:
    """Every key path that reaches an anchored or aliased node, or a merge.

    The editor shows these as hand-edited only: a write there would change
    every place the anchor is used, or edit a key that is not where it seems.
    """
    shared = shared_nodes(root)
    found: list[list[PathPart]] = []
    stack: list[tuple[yaml.Node, list[PathPart]]] = [(root, [])]
    while stack:
        node, path = stack.pop()
        merges = isinstance(node, yaml.MappingNode) and any(
            key.tag == _MERGE_TAG for key, _ in node.value
        )
        if id(node) in shared or merges:
            found.append(path)
            continue
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                name = _key_of(key)
                if isinstance(name, str):
                    stack.append((value, [*path, name]))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend((item, [*path, i]) for i, item in enumerate(node.value))
    return sorted(found, key=lambda p: [str(part) for part in p])
