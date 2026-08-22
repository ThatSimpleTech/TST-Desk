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

import yaml

from .config import TIER_NAMES, ConfigError, McpConfig, ensure_user_config, load_config

# Server names become part of tool names on the wire, so they are held to
# the same character set the config schema enforces (TD-4403).
_MCP_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


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


# ── MCP servers (TD-4403) ─────────────────────────────────────────────────
#
# The settings screen's add / disable / remove. Same surgical discipline as
# the tier writers, with one extra shape to handle: the shipped config
# carries an inline-empty ``servers: {}``, which grows into a block on the
# first edit. A replaced entry is written wholesale — command only — so a
# hand-added ``url:`` or stale ``enabled:`` can never survive next to a new
# command and break the one-transport rule.


def _locate_mcp_servers(lines: list[str]) -> tuple[int, int] | None:
    """``(index, end)`` of the ``servers:`` key under top-level ``mcp:``.

    ``end`` is one past the block's last line, per ``_block_bounds``. An
    inline ``servers: {}`` yields an empty body. ``None`` when there is no
    ``mcp:`` block or no ``servers:`` key inside it.
    """
    mcp_at = _find_key(lines, 0, len(lines), "mcp", 0)
    if mcp_at < 0:
        return None
    mcp_end = _block_bounds(lines, mcp_at, 0)
    servers_at = _find_key(lines, mcp_at + 1, mcp_end, "servers", 2)
    if servers_at < 0:
        return None
    return servers_at, _block_bounds(lines, servers_at, 2)


def _server_block(
    lines: list[str], servers_at: int, servers_end: int, name: str
) -> tuple[int, int] | None:
    """``(index, end)`` of one server's sub-block at indent 4, or ``None``."""
    at = _find_key(lines, servers_at + 1, servers_end, name, 4)
    if at < 0:
        return None
    return at, _block_bounds(lines, at, 4)


def _validate_mcp_entry(name: str, command: list[str]) -> list[str]:
    """Shared argument checks for the MCP writers; returns the cleaned argv."""
    if not isinstance(name, str) or not _MCP_NAME_RE.fullmatch(name):
        raise ConfigError("A server name uses only letters, digits, '_' and '-'")
    if not command or not all(isinstance(a, str) and a.strip() for a in command):
        raise ConfigError("A server command is a non-empty argv of non-empty strings")
    return command


def _write_mcp_lines(config_path: Path, lines: list[str]) -> Path:
    """Validate the edited document parses and its ``mcp`` section still
    satisfies the schema, then write it atomically.

    The check runs before the replace: an edit that would leave a broken
    config on disk raises instead, and the user's file keeps working.
    """
    text = "\n".join(lines)
    try:
        data = yaml.safe_load(text)
        McpConfig.model_validate((data or {}).get("mcp") or {})
    except Exception as e:
        raise ConfigError(f"The edit would not produce a valid mcp section: {e}") from e
    _atomic_write(config_path, text)
    return config_path


def save_mcp_server(name: str, command: list[str], path: Path | None = None) -> Path:
    """Add or replace one stdio server under ``mcp.servers`` (TD-4403).

    The entry is written as exactly ``command:`` plus its argv — JSON-encoded,
    which is always a valid YAML flow sequence and cannot inject structure —
    replacing any previous block under the same name. Values a user added by
    hand (a ``url:``, a tweaked ``enabled:``) do not survive the replace; the
    settings form owns the whole entry it writes.

    Returns the path written. Raises ``ConfigError`` for a bad name or argv,
    when the file has no ``mcp:`` block to extend, or when the edit would
    not validate.
    """
    argv = _validate_mcp_entry(name, command)
    entry = [f"    {name}:", f"      command: {json.dumps(argv)}"]

    config_path = ensure_user_config(path)
    lines = config_path.read_text(encoding="utf-8").split("\n")
    mcp_at = _find_key(lines, 0, len(lines), "mcp", 0)
    if mcp_at < 0:
        raise ConfigError(f"{config_path} has no `mcp:` block; add one by hand first")
    located = _locate_mcp_servers(lines)
    if located is None:
        # `mcp:` exists but carries no `servers:` key — insert one directly
        # under it, before whatever else the block holds.
        lines[mcp_at + 1 : mcp_at + 1] = ["  servers:", *entry]
    else:
        servers_at, servers_end = located
        existing = _server_block(lines, servers_at, servers_end, name)
        if existing is not None:
            start, end = existing
            lines[start:end] = entry
        elif lines[servers_at].strip() == "servers: {}":
            # First entry turns the inline-empty form into a block.
            lines[servers_at : servers_at + 1] = ["  servers:", *entry]
        else:
            lines[servers_end:servers_end] = entry
    return _write_mcp_lines(config_path, lines)


def save_mcp_enabled(name: str, enabled: bool, path: Path | None = None) -> Path:
    """Set ``enabled:`` on one configured server (TD-4403).

    The line is replaced where it sits, or appended at the end of the
    server's block when the entry never carried one. Returns the path
    written; raises ``ConfigError`` for an unknown server.
    """
    config_path = ensure_user_config(path)
    lines = config_path.read_text(encoding="utf-8").split("\n")
    located = _locate_mcp_servers(lines)
    if located is None:
        raise ConfigError(f"{config_path} has no `mcp.servers` mapping to edit")
    servers_at, servers_end = located
    block = _server_block(lines, servers_at, servers_end, name)
    if block is None:
        known = sorted(
            m.group(1)
            for m in (
                re.match(r"    ([A-Za-z0-9_-]+):", line)
                for line in lines[servers_at + 1 : servers_end]
            )
            if m
        )
        raise ConfigError(f"Unknown MCP server {name!r}; configured: {', '.join(known) or 'none'}")

    start, end = block
    new_line = f"      enabled: {json.dumps(enabled)}"
    enabled_at = _find_key(lines, start + 1, end, "enabled", 6)
    if enabled_at >= 0:
        lines[enabled_at] = new_line
    else:
        lines.insert(end, new_line)
    return _write_mcp_lines(config_path, lines)


def remove_mcp_server(name: str, path: Path | None = None) -> Path:
    """Remove one server's block from ``mcp.servers`` (TD-4403).

    The sub-block is deleted whole — the entry's lines are the ones deeper
    than its key. When the last server goes, ``servers:`` collapses back to
    the shipped inline-empty form. Returns the path written; raises
    ``ConfigError`` for an unknown server.
    """
    config_path = ensure_user_config(path)
    lines = config_path.read_text(encoding="utf-8").split("\n")
    located = _locate_mcp_servers(lines)
    if located is None:
        raise ConfigError(f"{config_path} has no `mcp.servers` mapping to edit")
    servers_at, servers_end = located
    block = _server_block(lines, servers_at, servers_end, name)
    if block is None:
        raise ConfigError(f"Unknown MCP server {name!r}")

    start, end = block
    del lines[start:end]
    servers_end -= end - start
    remaining = [
        line
        for line in lines[servers_at + 1 : servers_end]
        if line.strip() and not line.strip().startswith("#")
    ]
    if not remaining:
        lines[servers_at:servers_end] = ["  servers: {}"]
    return _write_mcp_lines(config_path, lines)
