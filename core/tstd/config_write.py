"""Writing configuration back to the user's ``config.yaml`` (TD-1101, TD-1703).

Split from :mod:`tstd.config` because loading and writing are different jobs:
that module parses and validates, this one edits a file a human also edits.

Every write here is surgical rather than a PyYAML dump, and for one reason:
the shipped config is mostly teaching. The block above ``local:`` explains
why ``context_window`` is a compaction budget and not a request parameter,
and a round-trip would drop all of it. So each function finds the one line it
owns and replaces it, leaving the rest of the document byte-identical.

Writes are atomic — same-directory temp file plus ``os.replace`` — so a crash
mid-write never leaves a torn config behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

from .config import TIER_NAMES, ConfigError, ensure_user_config, load_config


def save_active_preset(name: str, path: Path | None = None) -> Path:
    """Persist ``active_preset: <name>`` in the user config (TD-1101).

    The shipped config is comment-heavy and survives a PyYAML round-trip
    poorly, so instead of dumping we surgically rewrite the single top-level
    ``active_preset:`` line — appending it when absent.  The write is atomic
    (same-directory temp file + ``os.replace``), so a crash mid-write never
    leaves a torn config.

    Returns the path written.  Raises ``ConfigError`` if the name is not a
    declared preset of the loaded config.
    """
    config_path = ensure_user_config(path)
    config = load_config(config_path)
    if name not in config.presets:
        raise ConfigError(
            f"Unknown preset {name!r}; declared presets: {', '.join(sorted(config.presets))}"
        )

    text = config_path.read_text(encoding="utf-8")
    new_line = f"active_preset: {name}"
    pattern = re.compile(r"^active_preset:.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + new_line + "\n"

    _atomic_write(config_path, text)
    return config_path


def _atomic_write(config_path: Path, text: str) -> None:
    """Replace *config_path*'s contents in one step.

    Same-directory temp file plus ``os.replace``, so a crash mid-write never
    leaves a torn config behind.
    """
    fd, tmp_name = tempfile.mkstemp(dir=config_path.parent, prefix=config_path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, config_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _block_bounds(lines: list[str], start: int, header_indent: int) -> int:
    """Index one past the last line belonging to the block opened at *start*.

    A block ends at the first later line that has content at or left of the
    header's own indent; blank lines and comments carry no indent of their
    own and so never end it.
    """
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent <= header_indent:
            return i
    return len(lines)


def _find_key(lines: list[str], lo: int, hi: int, key: str, indent: int) -> int:
    """Index of ``key:`` between *lo* and *hi* at exactly *indent*, or -1."""
    for i in range(lo, hi):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) != indent:
            continue
        if line.strip().split(":", 1)[0].strip() == key:
            return i
    return -1


def save_tier_slug(preset: str, tier: str, slug: str, path: Path | None = None) -> Path:
    """Persist *slug* for *preset*'s *tier* in the user config (TD-1703).

    Surgical for the same reason as :func:`save_active_preset`: the shipped
    config is comment-heavy — most of what it teaches lives in the comments
    around ``context_window`` — and a PyYAML round-trip would drop every one
    of them.  The slug ships commented out (``# slug: your-model-tag``), so
    the placeholder is replaced where it sits and a line is inserted at the
    tier's own indent otherwise.

    The value is written JSON-encoded, which is always a valid YAML
    double-quoted scalar.  Model tags carry colons (``qwen3.8:27b``) and a
    raw newline would inject arbitrary YAML into the user's config, so the
    quoting is a boundary, not a style choice.

    Returns the path written.  Raises ``ConfigError`` if the preset or tier
    is not declared, if the slug is blank, or if the file's shape is not the
    one described above.

    Note it loads before it writes, so it cannot repair a config that a
    missing slug already made invalid — an off-box tier without one fails
    validation.  Unreachable from the settings screen, which only ever edits
    a config the daemon already loaded, but it does mean this is not a
    recovery tool.
    """
    cleaned = slug.strip()
    if not cleaned:
        raise ConfigError("A model slug cannot be blank; remove the key to fall back to discovery")

    config_path = ensure_user_config(path)
    config = load_config(config_path)
    if preset not in config.presets:
        raise ConfigError(
            f"Unknown preset {preset!r}; declared presets: {', '.join(sorted(config.presets))}"
        )
    if tier not in TIER_NAMES:
        raise ConfigError(f"Unknown tier {tier!r}; tiers are: {', '.join(TIER_NAMES)}")

    lines = config_path.read_text(encoding="utf-8").split("\n")
    presets_at = _find_key(lines, 0, len(lines), "presets", 0)
    if presets_at < 0:
        raise ConfigError(f"{config_path} has no top-level `presets:` block to edit")

    presets_end = _block_bounds(lines, presets_at, 0)
    preset_at = _find_key(lines, presets_at + 1, presets_end, preset, 2)
    if preset_at < 0:
        raise ConfigError(f"{config_path} declares no `{preset}:` block under `presets:`")

    preset_end = _block_bounds(lines, preset_at, 2)
    tier_at = _find_key(lines, preset_at + 1, preset_end, tier, 4)
    if tier_at < 0:
        raise ConfigError(f"{config_path} declares no `{tier}:` block under `presets: {preset}:`")

    tier_end = _block_bounds(lines, tier_at, 4)
    body_indent = 6
    for i in range(tier_at + 1, tier_end):
        if lines[i].strip() and not lines[i].strip().startswith("#"):
            body_indent = len(lines[i]) - len(lines[i].lstrip())
            break

    new_line = f"{' ' * body_indent}slug: {json.dumps(cleaned)}"
    live_at = _find_key(lines, tier_at + 1, tier_end, "slug", body_indent)
    if live_at >= 0:
        lines[live_at] = new_line
    else:
        # The commented placeholder marks where the author meant it to go.
        placeholder = next(
            (
                i
                for i in range(tier_at + 1, tier_end)
                if lines[i].lstrip().startswith("#") and "slug:" in lines[i]
            ),
            -1,
        )
        if placeholder >= 0:
            lines[placeholder] = new_line
        else:
            lines.insert(tier_at + 1, new_line)

    _atomic_write(config_path, "\n".join(lines))
    return config_path
