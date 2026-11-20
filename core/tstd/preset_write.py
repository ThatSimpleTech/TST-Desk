"""Copy, relabel and remove catalog entries in the user's ``config.yaml`` (TD-5237).

The walkthrough could only add a model, so the way to try a variation was
to overwrite the one entry already there. These three writers let Settings
keep several: copy an entry, give one a new label, remove one.

Like :mod:`tstd.config_write`, every edit is surgical: the lines the entry
owns change and the rest of the file stays byte-identical. Unlike a single
value, an entry can be tied to others by a YAML anchor (``&my_model`` in
one, ``*my_model`` in the next), so each would-be file is loaded exactly as
the daemon loads it *before* it is written. An edit that would leave a file
that does not load is refused, and nothing is written.

An entry's key is its permanent id: sessions, scheduled jobs and
``computer_use.local_worker_preset`` all store it. So "rename" here changes
the ``label`` a person sees and never the key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from .config import (
    CREDENTIAL_ID_RE,
    ConfigError,
    ModelConfig,
    config_from_data,
    ensure_user_config,
    load_config,
)
from .config_write import _atomic_write, _block_bounds, _find_key

_ENTRY_INDENT_DEFAULT = 2


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _content_end(lines: list[str], start: int, end: int) -> int:
    """One past the last content line in ``[start, end)``.

    A block's bounds run on through blank lines and comments, and a comment
    there usually introduces the *next* section. It stays with that section.
    """
    last = start
    for i in range(start, end):
        if _is_content(lines[i]):
            last = i
    return last + 1


def _presets_block(lines: list[str], config_path: Path) -> tuple[int, int, int]:
    """``(header, end, entry_indent)`` of the top-level ``presets:`` block."""
    header = _find_key(lines, 0, len(lines), "presets", 0)
    if header < 0 or lines[header].split(":", 1)[1].split("#", 1)[0].strip():
        # Missing, or written inline (``presets: {a: ...}``).
        raise ConfigError(f"{config_path} has a `presets:` value this writer cannot edit")
    end = _block_bounds(lines, header, 0)
    for i in range(header + 1, end):
        if _is_content(lines[i]):
            return header, end, _indent(lines[i])
    return header, end, _ENTRY_INDENT_DEFAULT


def _entry_bounds(
    lines: list[str], name: str, config_path: Path
) -> tuple[int, int, int, tuple[int, int]]:
    """``(start, end, entry_indent, (presets_header, presets_end))`` for *name*."""
    header, end, entry_indent = _presets_block(lines, config_path)
    start = _find_key(lines, header + 1, end, name, entry_indent)
    if start < 0:
        # Declared (the caller checked) but not as a plain ``name:`` line,
        # e.g. a quoted key or a merge from elsewhere.
        raise ConfigError(f"{config_path} declares {name!r} in a way this writer cannot edit")
    block_end = min(_block_bounds(lines, start, entry_indent), end)
    return start, _content_end(lines, start, block_end), entry_indent, (header, end)


def _checked(text: str, config_path: Path, why: str) -> ModelConfig:
    """Load the would-be file as the daemon would, or refuse the edit."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{why} ({e})") from e
    if not isinstance(data, dict):
        raise ConfigError(why)
    try:
        return config_from_data(data, config_path)
    except ConfigError as e:
        raise ConfigError(f"{why} ({e})") from e


def _require(config: ModelConfig, name: str) -> None:
    if name not in config.presets:
        declared = ", ".join(sorted(config.presets)) or "none"
        raise ConfigError(f"Unknown preset {name!r}; declared presets: {declared}")


def _clean_label(label: str) -> str:
    cleaned = label.strip()
    if not cleaned:
        raise ConfigError("Give the model a name")
    if len(cleaned) > 40:
        raise ConfigError("A model's name can be at most 40 characters")
    return cleaned


def _yaml_block(value: dict[str, Any], indent: int) -> list[str]:
    """*value* as block-style YAML lines, each indented by *indent* spaces."""
    dumped = yaml.safe_dump(
        value, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000
    )
    pad = " " * indent
    return [pad + line if line else line for line in dumped.rstrip("\n").split("\n")]


def duplicate_preset(source: str, name: str, label: str, path: Path | None = None) -> Path:
    """Write a copy of *source* as a new entry *name* labelled *label*.

    The copy is written from the loaded entry, not from its text: every
    role is spelled out in full, so it shares no YAML anchor with the
    original and an edit to one never changes the other. Only keys the
    original set are written; defaults stay defaults. It lands straight
    after the last entry. Never writes a secret: a role's ``credential`` is
    a catalog id.

    Raises ``ConfigError`` when *source* is not declared, *name* is taken
    or not a slug, or the result would not load.
    """
    if not CREDENTIAL_ID_RE.match(name):
        raise ConfigError(f"preset name {name!r} must be a lowercase slug [a-z][a-z0-9-]{{0,31}}")
    cleaned = _clean_label(label)
    config_path = ensure_user_config(path)
    config = load_config(config_path)
    _require(config, source)
    if name in config.presets:
        raise ConfigError(f"A preset named {name!r} already exists")

    lines = config_path.read_text(encoding="utf-8").split("\n")
    header, end, entry_indent = _presets_block(lines, config_path)
    body = config.presets[source].model_dump(mode="json", exclude_unset=True)
    body.pop("label", None)
    block = [
        f"{' ' * entry_indent}{name}:",
        f"{' ' * (entry_indent * 2)}label: {json.dumps(cleaned)}",
        *_yaml_block(body, entry_indent * 2),
    ]
    at = _content_end(lines, header, end)
    lines[at:at] = block

    text = "\n".join(lines)
    loaded = _checked(text, config_path, f"Could not copy {source!r}")
    if name not in loaded.presets:
        raise ConfigError(f"Could not copy {source!r}: the copy did not load as an entry")
    _atomic_write(config_path, text)
    return config_path


def save_preset_label(
    name: str, label: str, description: str | None = None, path: Path | None = None
) -> Path:
    """Set the ``label`` of entry *name*, and its ``description`` when given.

    *description* None leaves the line as it is; an empty one removes it.
    The entry's key does not change. Strings are written JSON-encoded, a
    valid YAML double-quoted scalar, so a label cannot inject YAML.
    """
    cleaned = _clean_label(label)
    if description is not None and len(description.strip()) > 120:
        raise ConfigError("A description can be at most 120 characters")
    config_path = ensure_user_config(path)
    _require(load_config(config_path), name)

    lines = config_path.read_text(encoding="utf-8").split("\n")
    start, end, entry_indent, _ = _entry_bounds(lines, name, config_path)
    key_indent = entry_indent * 2
    for i in range(start + 1, end):
        if _is_content(lines[i]):
            key_indent = _indent(lines[i])
            break
    pad = " " * key_indent

    wanted: list[tuple[str, str | None]] = [("label", cleaned)]
    if description is not None:
        wanted.append(("description", description.strip() or None))
    insert_at = start + 1
    for key, value in wanted:
        at = _find_key(lines, start + 1, end, key, key_indent)
        if at >= 0 and re.search(r":\s*[|>&*]", lines[at]):
            raise ConfigError(f"{config_path} writes {name}.{key} in a way this writer cannot edit")
        if value is None:
            if at >= 0:
                del lines[at]
                end -= 1
            continue
        line = f"{pad}{key}: {json.dumps(value)}"
        if at >= 0:
            lines[at] = line
            insert_at = at + 1
        else:
            lines.insert(insert_at, line)
            insert_at += 1
            end += 1

    text = "\n".join(lines)
    loaded = _checked(text, config_path, f"Could not rename {name!r}")
    if loaded.presets[name].label != cleaned:
        raise ConfigError(f"Could not rename {name!r}: the file sets its label somewhere else")
    _atomic_write(config_path, text)
    return config_path


def delete_preset(name: str, path: Path | None = None) -> Path:
    """Remove entry *name* from the catalog.

    Refused while ``computer_use.local_worker_preset`` names it, and when
    another entry reuses part of it through a YAML anchor: the file would
    no longer load. When *name* was ``active_preset``, the first entry left
    becomes the active one (none left: the key is cleared), the same choice
    the loader would make. Removing the last entry leaves ``presets: {}``.
    """
    config_path = ensure_user_config(path)
    config = load_config(config_path)
    _require(config, name)
    if config.computer_use.local_worker_preset.strip() == name:
        raise ConfigError(
            f"{name!r} is the computer-use helper model (computer_use.local_worker_preset). "
            "Point that at another model first"
        )

    lines = config_path.read_text(encoding="utf-8").split("\n")
    start, end, _entry_indent, (header, _presets_end) = _entry_bounds(lines, name, config_path)
    del lines[start:end]
    remaining = [n for n in config.presets if n != name]
    if not remaining:
        lines[header] = "presets: {}"

    text = "\n".join(lines)
    if config.active_preset == name:
        successor = remaining[0] if remaining else ""
        active_line = f"active_preset: {successor}" if successor else 'active_preset: ""'
        active = re.compile(r"^active_preset:.*$", re.MULTILINE)
        if active.search(text):
            text = active.sub(active_line, text, count=1)

    loaded = _checked(
        text,
        config_path,
        f"Could not remove {name!r}: another model in the file reuses part of it. "
        "Edit config.yaml by hand",
    )
    if name in loaded.presets:
        raise ConfigError(f"Could not remove {name!r}: the file declares it more than once")
    _atomic_write(config_path, text)
    return config_path
