"""Write a tier's card values into the user's ``config.yaml`` (TD-5185).

Surgical, like every writer in :mod:`tstd.config_write`: the file is mostly
teaching comments that a PyYAML round-trip would drop, so this finds each
value's own line and replaces only the value, keeping any comment at the end
of the line. A tier's backups (``fallbacks``, TD-5120) are list entries one
level down and are edited the same way, inside their own entry only.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import ConfigError, ensure_user_config
from .config_write import _atomic_write, _block_bounds, _find_key

_VALUE_LINE = re.compile(
    r"^(?P<head>\s*(?:-\s+)?[A-Za-z_]+:\s*)(?P<value>[^#]*?)(?P<tail>\s+#.*)?$"
)


def format_value(value: float | int) -> str:
    """A YAML number PyYAML reads back as the same value.

    Fixed point, never ``1e-06``: PyYAML's YAML 1.1 resolver reads that
    spelling as a string.
    """
    if isinstance(value, bool):
        raise ConfigError("A card value cannot be a boolean")
    if isinstance(value, int):
        return str(value)
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def save_tier_values(
    preset: str,
    tier: str,
    values: dict[str, float | int],
    *,
    fallback: int | None = None,
    path: Path | None = None,
) -> Path:
    """Set *values* on *preset*'s *tier*, or on its backup number *fallback*.

    A key already present has its value replaced in place; a missing one is
    added at the block's own indent. Raises ``ConfigError`` when the file
    does not have the block, so a hand-edited shape this cannot read is
    reported instead of guessed at.
    """
    config_path = ensure_user_config(path)
    lines = config_path.read_text(encoding="utf-8").split("\n")
    lo, hi, indent = _tier_block(lines, preset, tier, config_path)
    if fallback is not None:
        lo, hi, indent = _fallback_block(lines, lo, hi, indent, fallback, config_path)
    for key, value in values.items():
        _set_key(lines, lo, hi, indent, key, format_value(value))
        # An inserted line moves the block's end by one.
        hi = _block_end(lines, lo, indent, fallback is not None)
    _atomic_write(config_path, "\n".join(lines))
    return config_path


def _tier_block(lines: list[str], preset: str, tier: str, where: Path) -> tuple[int, int, int]:
    """``(header, end, body indent)`` of ``presets.<preset>.<tier>``."""
    presets_at = _find_key(lines, 0, len(lines), "presets", 0)
    if presets_at < 0:
        raise ConfigError(f"{where} has no top-level `presets:` block to edit")
    presets_end = _block_bounds(lines, presets_at, 0)
    preset_at = _find_key(lines, presets_at + 1, presets_end, preset, 2)
    if preset_at < 0:
        raise ConfigError(f"{where} declares no `{preset}:` block under `presets:`")
    preset_end = _block_bounds(lines, preset_at, 2)
    tier_at = _find_key(lines, preset_at + 1, preset_end, tier, 4)
    if tier_at < 0:
        raise ConfigError(f"{where} declares no `{tier}:` block under `presets: {preset}:`")
    tier_end = _block_bounds(lines, tier_at, 4)
    return tier_at, tier_end, _body_indent(lines, tier_at, tier_end, 6)


def _fallback_block(
    lines: list[str], lo: int, hi: int, indent: int, index: int, where: Path
) -> tuple[int, int, int]:
    """``(dash line, end, key indent)`` of backup *index* inside a tier block."""
    list_at = _find_key(lines, lo + 1, hi, "fallbacks", indent)
    if list_at < 0:
        raise ConfigError(f"{where}: the tier has no `fallbacks:` block")
    list_end = _block_bounds(lines, list_at, indent)
    dashes = [
        i
        for i in range(list_at + 1, list_end)
        if lines[i].lstrip().startswith("- ")
        and _indent(lines[i]) == _indent(lines[_first_dash(lines, list_at, list_end)])
    ]
    if index >= len(dashes):
        raise ConfigError(f"{where}: `fallbacks` has no entry {index} written as a block list")
    start = dashes[index]
    key_indent = _indent(lines[start]) + 2
    return start, _block_end(lines, start, key_indent, True), key_indent


def _first_dash(lines: list[str], lo: int, hi: int) -> int:
    for i in range(lo + 1, hi):
        if lines[i].lstrip().startswith("- "):
            return i
    return lo


def _block_end(lines: list[str], start: int, key_indent: int, is_entry: bool) -> int:
    """One past the block opened at *start* whose keys sit at *key_indent*."""
    if not is_entry:
        return _block_bounds(lines, start, _indent(lines[start]))
    dash_indent = key_indent - 2
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(lines[i]) <= dash_indent:
            return i
    return len(lines)


def _body_indent(lines: list[str], lo: int, hi: int, default: int) -> int:
    for i in range(lo + 1, hi):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return _indent(lines[i])
    return default


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _key_of(line: str) -> str:
    return line.strip().removeprefix("- ").split(":", 1)[0].strip()


def _set_key(lines: list[str], lo: int, hi: int, indent: int, key: str, text: str) -> None:
    """Replace *key*'s value inside ``lines[lo:hi]``, or add it after *lo*."""
    for i in range(lo, hi):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # The dash line of a backup carries its first key after the "- ".
        own_indent = _indent(line) + 2 if stripped.startswith("- ") else _indent(line)
        if own_indent != indent or _key_of(line) != key:
            continue
        found = _VALUE_LINE.match(line)
        if found is None:
            raise ConfigError(f"Cannot read the `{key}:` line to update it")
        lines[i] = f"{found['head']}{text}{found['tail'] or ''}"
        return
    lines.insert(lo + 1, f"{' ' * indent}{key}: {text}")
