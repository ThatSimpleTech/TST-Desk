"""Settings → Configuration: config.yaml as the editor sees it, and one-key edits (TD-5203).

The view is built from the file, not from the daemon's live config: the
live one carries slugs discovery filled in and the remote bind the switch
chose, and the editor must show what the file says. A ``secrets`` map's
values never leave here (each becomes where it lives), and a password in a
URL is masked.

An edit is planned on the file's text (:mod:`tstd.config_yaml_edit`),
then the whole would-be file is parsed and loaded the way the daemon loads
it, and checked by the same guards the dedicated Settings writers apply.
Only then is it written, atomically. Any failure raises
:class:`ConfigEditRefused` and the file is untouched. The checks themselves
are in :mod:`tstd.config_editor_checks`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from .config import (
    FILLED_FROM_SHIPPED,
    ConfigError,
    ModelConfig,
    config_from_data,
    default_config_yaml,
    ensure_user_config,
)
from .config_editor_checks import (
    ConfigEditRefused,
    check_guards,
    check_value,
    dotted,
    fields_of,
)
from .config_editor_wire import ConfigNode
from .config_schema import ROOT_MODEL
from .config_write import _atomic_write
from .config_yaml_edit import remove_value, set_value, top_level_block
from .config_yaml_emit import PathPart, YamlEditError
from .logging import REDACTED
from .mcp_values import keychain_entry

Action = Literal["set", "reset"]

#: ``scheme://user:password@`` — the password part is masked on the wire.
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]*):[^/\s@]+@")
_ROOT = ConfigNode(kind="object", ref=ROOT_MODEL)


@dataclass(frozen=True)
class EditorView:
    """What a ``config_editor`` event carries besides the static schema."""

    values: dict[str, Any]
    set_paths: list[list[PathPart]]
    version: str
    file: str


@dataclass(frozen=True)
class _Target:
    node: ConfigNode
    trail: list[ConfigNode]
    #: The map or list the last step went into, when the path ends at an entry.
    container: ConfigNode | None
    #: Whether any step went into a map entry or list item.
    inside_entry: bool


def version_of(text: str) -> str:
    """A short fingerprint of the file's text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def path_key(path: list[PathPart]) -> str:
    """How ``defaults`` names a path: compact JSON, as the window's JSON.stringify."""
    return json.dumps(path, separators=(",", ":"), ensure_ascii=False)


def _parse(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigEditRefused("invalid", f"config.yaml is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigEditRefused("invalid", "config.yaml must be a mapping at the top level")
    return data


def _has(data: Any, path: list[PathPart]) -> bool:
    for part in path:
        in_map = isinstance(data, dict) and isinstance(part, str) and part in data
        in_list = isinstance(data, list) and isinstance(part, int) and 0 <= part < len(data)
        if not (in_map or in_list):
            return False
        data = data[part]
    return True


def _get(data: Any, path: list[PathPart]) -> Any:
    for part in path:
        data = data[part]
    return data


def _paths_of(data: Any, prefix: list[PathPart]) -> list[list[PathPart]]:
    found: list[list[PathPart]] = []
    items: list[tuple[PathPart, Any]] = []
    if isinstance(data, dict):
        items = [(k, v) for k, v in data.items() if isinstance(k, str)]
    elif isinstance(data, list):
        items = list(enumerate(data))
    for part, child in items:
        path = [*prefix, part]
        found.append(path)
        found.extend(_paths_of(child, path))
    return found


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_PASSWORD.sub(rf"\1:{REDACTED}@", value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _masked(node: ConfigNode, value: Any) -> Any:
    """*value* fit for the wire: secrets as where they live, URL passwords hidden."""
    if node.kind == "secrets" and isinstance(value, dict):
        return {
            name: "keychain" if isinstance(v, str) and keychain_entry(v) is not None else "config"
            for name, v in value.items()
        }
    if node.kind == "object" and isinstance(value, dict):
        fields = fields_of(node)
        return {k: _masked(fields[k], v) if k in fields else _scrub(v) for k, v in value.items()}
    if node.kind == "map" and node.entries is not None and isinstance(value, dict):
        return {k: _masked(node.entries, v) for k, v in value.items()}
    if node.kind == "list" and node.items is not None and isinstance(value, list):
        return [_masked(node.items, v) for v in value]
    return _scrub(value)


def build_view(config_path: Path) -> EditorView:
    """The file's effective values, the keys it sets, and its version."""
    config_path = ensure_user_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    raw = _parse(text)
    try:
        config = config_from_data(copy.deepcopy(raw), config_path)
    except ConfigError as e:
        raise ConfigEditRefused("invalid", str(e)) from e
    values = _masked(_ROOT, config.model_dump(mode="json"))
    return EditorView(
        values=values,
        set_paths=_paths_of(raw, []),
        version=version_of(text),
        file=str(config_path),
    )


def _static_leaves(
    node: ConfigNode, path: list[PathPart]
) -> list[tuple[list[PathPart], ConfigNode]]:
    """Keys at a fixed place: not inside a list item or map entry."""
    if node.kind == "object" and not node.nullable:
        return [
            leaf
            for name, child in fields_of(node).items()
            for leaf in _static_leaves(child, [*path, name])
        ]
    holds_entries = node.kind in ("map", "list") and any(
        n is not None and n.kind == "object" for n in (node.entries, node.items)
    )
    return [] if holds_entries or not path else [(path, node)]


@lru_cache(maxsize=1)
def _shipped_raw() -> dict[str, Any]:
    return _parse(default_config_yaml())


@lru_cache(maxsize=1)
def shipped_defaults() -> dict[str, Any]:
    """Each fixed key's reset value: what a fresh install's file loads as."""
    shipped = config_from_data(copy.deepcopy(_shipped_raw()), Path("config.yaml"))
    dump = shipped.model_dump(mode="json")
    return {
        path_key(path): _get(dump, path)
        for path, node in _static_leaves(_ROOT, [])
        if node.resettable
    }


def _resolve(path: list[PathPart]) -> _Target:
    node, trail = _ROOT, []
    container: ConfigNode | None = None
    inside = False
    for i, part in enumerate(path):
        if node.kind == "secrets":
            raise ConfigEditRefused("secret", node.note)
        if node.kind == "object" and isinstance(part, str) and part in fields_of(node):
            node, container = fields_of(node)[part], None
        elif node.kind == "map" and isinstance(part, str) and node.entries is not None:
            node, container, inside = node.entries, node, True
        elif node.kind == "list" and isinstance(part, int) and node.items is not None:
            node, container, inside = node.items, node, True
        else:
            raise ConfigEditRefused("not_found", f"config.yaml has no key {dotted(path[: i + 1])}")
        trail.append(node)
    for step in trail:
        if step.kind == "secrets":
            raise ConfigEditRefused("secret", step.note)
        if not step.editable:
            raise ConfigEditRefused("not_editable", step.note)
    return _Target(node=node, trail=trail, container=container, inside_entry=inside)


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


def _plan_set(text: str, raw: dict[str, Any], path: list[PathPart], value: Any) -> str:
    target = _resolve(path)
    node = target.node
    where = dotted(path)
    holds_entries = node.kind in ("map", "list") and any(
        n is not None and n.kind == "object" for n in (node.entries, node.items)
    )
    if holds_entries:
        raise ConfigEditRefused("not_editable", f"{where}: add or remove one entry at a time")
    if node.kind == "object" and not node.nullable and target.container is None:
        raise ConfigEditRefused("not_editable", f"{where}: change one key at a time")
    exists = _has(raw, path)
    if target.container is not None and not exists and not target.container.addable:
        raise ConfigEditRefused("not_editable", target.container.add_note)
    # A group is written whole only when it is new (an added entry, a group
    # turned on). Over one that exists it would drop the keys the editor never
    # writes, such as an MCP server's keychain references.
    if node.kind == "object" and value is not None and exists and _get(raw, path) is not None:
        raise ConfigEditRefused("not_editable", f"{where}: change one key at a time")
    check_value(node, value, where)
    if exists and _get(raw, path) == value:
        return text
    text = _materialized(text, raw, path[0])
    return set_value(text, path, value, flow=node.kind == "json")


def _plan_reset(text: str, raw: dict[str, Any], path: list[PathPart]) -> str:
    target = _resolve(path)
    node = target.node
    if target.container is not None:
        if not target.container.removable:
            raise ConfigEditRefused("not_editable", target.container.remove_note)
        return remove_value(text, path)
    if not node.resettable:
        raise ConfigEditRefused("not_editable", f"{dotted(path)} is set by its own control")
    if target.inside_entry:
        if not node.has_default:
            raise ConfigEditRefused(
                "not_editable", f"{dotted(path)} has no default; remove the entry instead"
            )
        return remove_value(text, path)
    section = path[0]
    if isinstance(section, str) and section not in raw and section in FILLED_FROM_SHIPPED:
        return text  # still the shipped section, so already the shipped value
    if _has(_shipped_raw(), path):
        shipped = _get(_shipped_raw(), path)
        if _has(raw, path) and _get(raw, path) == shipped:
            return text
        return set_value(text, path, shipped, flow=node.kind == "json")
    return remove_value(text, path)


def apply_edit(
    config_path: Path,
    action: Action,
    path: list[PathPart],
    value: Any = None,
    version: str = "",
) -> ModelConfig | None:
    """Make one edit, or raise :class:`ConfigEditRefused` with the file untouched.

    Returns the loaded would-be config when the file changed, and None
    when the edit asked for what the file already says. *version* is
    checked when the path goes through a list position: the item there
    may have moved since the window last read the file.
    """
    config_path = ensure_user_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    raw = _parse(text)
    if any(isinstance(p, int) for p in path) and version != version_of(text):
        raise ConfigEditRefused("stale", "config.yaml changed since this page loaded; try again")
    try:
        if action == "set":
            new_text = _plan_set(text, raw, path, value)
        else:
            new_text = _plan_reset(text, raw, path)
    except YamlEditError as e:
        raise ConfigEditRefused("write_failed", str(e)) from e
    if new_text == text:
        return None
    new_raw = _parse(new_text)
    if action == "set" and (not _has(new_raw, path) or _get(new_raw, path) != value):
        raise ConfigEditRefused("write_failed", "the value did not read back as written")
    try:
        config = config_from_data(copy.deepcopy(new_raw), config_path)
    except ConfigError as e:
        prefix = f"Invalid model configuration in {config_path}: "
        # Pydantic labels a validator's own words "Value error, "; the words are enough.
        detail = str(e).removeprefix(prefix).replace(": Value error, ", ": ")
        raise ConfigEditRefused("invalid", detail) from e
    check_guards(config, new_raw, path)
    try:
        _atomic_write(config_path, new_text)
    except OSError as e:
        raise ConfigEditRefused("write_failed", f"could not write config.yaml: {e}") from e
    return config
