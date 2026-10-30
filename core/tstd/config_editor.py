"""Settings → Configuration: config.yaml as the editor sees it, and one-key edits (TD-5203).

The view is built from the file, not from the daemon's live config: the
live one carries slugs discovery filled in and the remote bind the switch
chose, and the editor must show what the file says. A ``secrets`` map's
values never leave here (each becomes where it lives), and the user info
of a URL (a password, or a token used as the user name) is masked.

An edit is planned on the file's text (:mod:`tstd.config_editor_plan`),
then the whole would-be file is parsed, compared with what it should read
back as, loaded the way the daemon loads it, and checked by the same
guards the dedicated Settings writers apply. Only then is it written,
atomically. Any failure raises :class:`ConfigEditRefused` and the file is
untouched. The checks themselves are in :mod:`tstd.config_editor_checks`.

The file is read and written byte for byte: its line endings stay as they
are, whatever the platform's.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from .config import ConfigError, ModelConfig, config_from_data, ensure_user_config
from .config_editor_checks import (
    ConfigEditRefused,
    check_guards,
    fields_of,
    mask_url_userinfo,
    scrub,
)
from .config_editor_plan import (
    ROOT,
    get,
    holds_entries,
    plan_reset,
    plan_set,
    same,
    shipped_raw,
)
from .config_editor_wire import ConfigNode
from .config_write import _atomic_write
from .config_yaml_emit import PathPart, YamlEditError
from .config_yaml_nodes import anchored_paths
from .logging import get_logger
from .mcp_values import keychain_entry

log = get_logger(__name__)

Action = Literal["set", "reset"]

#: The largest config.yaml an edit may leave. The shipped file is about
#: 30 KB; each reload and each view parses the whole file on the loop.
MAX_FILE_CHARS = 1 << 20

#: When the splicer's own output does not parse, the person's file is not
#: at fault, and PyYAML's snippet of the text could show a value the view
#: masks. The detail goes to the local log instead.
_NOT_IN_PLACE = "this key could not be written in place; edit config.yaml by hand"


@dataclass(frozen=True)
class EditorView:
    """What a ``config_editor`` event carries besides the static schema."""

    values: dict[str, Any]
    set_paths: list[list[PathPart]]
    locked_paths: list[list[PathPart]]
    version: str
    file: str


def version_of(text: str) -> str:
    """A short fingerprint of the file's text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def path_key(path: list[PathPart]) -> str:
    """How ``defaults`` names a path: compact JSON, as the window's JSON.stringify."""
    return json.dumps(path, separators=(",", ":"), ensure_ascii=False)


def _read(config_path: Path) -> str:
    """The file's text exactly: no newline translation, so its fingerprint is its bytes."""
    try:
        return config_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigEditRefused(
            "invalid", "config.yaml is not UTF-8 text; fix it by hand"
        ) from None


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


def _parse_written(text: str) -> dict[str, Any]:
    """The would-be file, parsed; a failure is the splice's, never the person's file."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        log.warning(
            "config edit produced text that does not parse",
            extra={
                "extra_fields": {
                    "problem": scrub(str(getattr(e, "problem", None) or type(e).__name__)),
                    "line": mark.line + 1 if mark is not None else None,
                }
            },
        )
        raise ConfigEditRefused("write_failed", _NOT_IN_PLACE) from None
    if not isinstance(data, dict):
        raise ConfigEditRefused("write_failed", _NOT_IN_PLACE)
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
        return mask_url_userinfo(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _masked(node: ConfigNode, value: Any) -> Any:
    """*value* fit for the wire: secrets as where they live, URL user info hidden."""
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


def _locked(text: str) -> list[list[PathPart]]:
    """Keys written through a YAML anchor, alias or merge: edited by hand only."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return []
    return [] if root is None else anchored_paths(root)


def build_view(config_path: Path) -> EditorView:
    """The file's effective values, the keys it sets, those it locks, and its version."""
    config_path = ensure_user_config(config_path)
    text = _read(config_path)
    raw = _parse(text)
    try:
        config = config_from_data(copy.deepcopy(raw), config_path)
    except ConfigError as e:
        raise ConfigEditRefused("invalid", str(e)) from e
    values = _masked(ROOT, config.model_dump(mode="json"))
    return EditorView(
        values=values,
        set_paths=_paths_of(raw, []),
        locked_paths=_locked(text),
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
    return [] if holds_entries(node) or not path else [(path, node)]


@lru_cache(maxsize=1)
def shipped_defaults() -> dict[str, Any]:
    """Each fixed key's reset value: what a fresh install's file loads as."""
    shipped = config_from_data(copy.deepcopy(shipped_raw()), Path("config.yaml"))
    dump = shipped.model_dump(mode="json")
    return {
        path_key(path): get(dump, path)
        for path, node in _static_leaves(ROOT, [])
        if node.resettable
    }


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
    text = _read(config_path)
    raw = _parse(text)
    if any(isinstance(p, int) for p in path) and version != version_of(text):
        raise ConfigEditRefused("stale", "config.yaml changed since this page loaded; try again")
    try:
        plan = plan_set(text, raw, path, value) if action == "set" else plan_reset(text, raw, path)
    except YamlEditError as e:
        raise ConfigEditRefused("write_failed", str(e)) from e
    if plan.text == text:
        return None
    if len(plan.text) > MAX_FILE_CHARS and len(plan.text) > len(text):
        raise ConfigEditRefused(
            "invalid", "config.yaml would grow past 1 MiB; keep a value this long in a file"
        )
    new_raw = _parse_written(plan.text)
    # The whole file, not just the key: a splice that touched a neighbour
    # (a key behind a line break only YAML saw) must never be written.
    if not same(new_raw, plan.expect):
        raise ConfigEditRefused("write_failed", "the value did not read back as written")
    try:
        config = config_from_data(copy.deepcopy(new_raw), config_path)
    except ConfigError as e:
        prefix = f"Invalid model configuration in {config_path}: "
        # Pydantic labels a validator's own words "Value error, "; the words are enough.
        detail = str(e).removeprefix(prefix).replace(": Value error, ", ": ")
        raise ConfigEditRefused("invalid", detail) from e
    check_guards(config, new_raw, path, raw)
    try:
        _atomic_write(config_path, plan.text, newline="")
    except OSError as e:
        raise ConfigEditRefused("write_failed", f"could not write config.yaml: {e}") from e
    return config
