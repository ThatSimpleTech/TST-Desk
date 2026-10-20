"""Settings verbs for one MCP server's env vars and headers (TD-5110).

Settings only ever writes a ``keychain:<entry>`` reference into
``config.yaml``; the value goes to the keychain first. A literal value
is something the user typed into ``config.yaml`` by hand, and Settings
shows its name only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from . import keychain_mcp
from .config import ConfigError, McpConfig, McpServerConfig
from .config_write import save_mcp_server
from .keychain import KeychainError
from .logging import get_logger
from .mcp_values import (
    ValueKind,
    check_name,
    check_secret,
    entry_for_name,
    keychain_entry,
    keychain_ref,
    label,
)
from .protocol import DeleteMcpSecret, McpValueSummary, SetMcpSecret

log = get_logger("tstd.mcp_settings")

StoredProbe = Callable[[str, str], Awaitable[bool]]


def _kind_for(server_id: str, spec: McpServerConfig, target: ValueKind) -> ValueKind:
    wanted: ValueKind = "env" if spec.transport == "stdio" else "header"
    if target != wanted:
        takes = "env" if wanted == "env" else "headers"
        raise ConfigError(f"MCP server {server_id!r} is {spec.transport}; it takes {takes}")
    return target


def _server(mcp: McpConfig, server_id: str) -> McpServerConfig:
    spec = mcp.servers.get(server_id)
    if spec is None:
        raise ConfigError(f"no MCP server {server_id!r} is listed")
    return spec


def _mapping(spec: McpServerConfig, kind: ValueKind) -> dict[str, str]:
    return dict(spec.env if kind == "env" else spec.headers)


def _find(mapping: Mapping[str, str], kind: ValueKind, name: str) -> str | None:
    """The spelling *name* already has here. Header names ignore case."""
    if name in mapping:
        return name
    if kind == "header":
        folded = name.lower()
        return next((key for key in mapping if key.lower() == folded), None)
    return None


def _free_entry(spec: McpServerConfig, name: str) -> str:
    """``entry_for_name``, with ``-2``, ``-3``… when another name has it."""
    taken = {
        entry
        for value in (*spec.env.values(), *spec.headers.values())
        if (entry := keychain_entry(value)) is not None
    }
    base = entry_for_name(name)
    entry, n = base, 1
    while entry in taken:
        n += 1
        suffix = f"-{n}"
        entry = f"{base[: 64 - len(suffix)]}{suffix}"
    return entry


def _with(spec: McpServerConfig, kind: ValueKind, mapping: dict[str, str]) -> McpServerConfig:
    key = "env" if kind == "env" else "headers"
    return McpServerConfig.model_validate({**spec.model_dump(), key: mapping})


async def set_mcp_secret(mcp: McpConfig, path: Path, msg: SetMcpSecret) -> str:
    """Store the value, then point config at it. Returns the entry.

    The keychain write comes first, so a failure leaves config.yaml as it
    was; if the file write then fails, the new item is deleted again, so
    no item is left that nothing names (TD-5160). A name that already
    names an entry keeps it and config is not rewritten.

    Raises:
        ConfigError: Unknown server, wrong target, bad name or value.
        KeychainError: The keychain refused the value.
    """
    spec = _server(mcp, msg.server_id)
    kind = _kind_for(msg.server_id, spec, msg.target)
    try:
        check_name(kind, msg.name)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    reason = check_secret(msg.value)
    if reason is not None:
        raise ConfigError(f"The value for {label(kind, msg.name)} {reason}.")
    mapping = _mapping(spec, kind)
    name = _find(mapping, kind, msg.name) or msg.name
    entry = keychain_entry(mapping.get(name, ""))
    rewrite = entry is None
    if entry is None:
        entry = _free_entry(spec, name)
    await keychain_mcp.store_mcp_secret(msg.server_id, entry, msg.value)
    if rewrite:
        mapping[name] = keychain_ref(entry)
        try:
            save_mcp_server(msg.server_id, _with(spec, kind, mapping), path)
        except Exception:
            await _forget_item(msg.server_id, entry)
            raise
    return entry


async def delete_mcp_secret(mcp: McpConfig, path: Path, msg: DeleteMcpSecret) -> None:
    """Delete the keychain item the name points at, then drop the name.

    The item goes first (TD-5160): if it will not delete, config.yaml
    still names it, so the file, the daemon's copy and the keychain agree
    and Remove can be tried again. The other order left an item nothing
    names, which removing the server never cleans up.

    Raises:
        ConfigError: Unknown server or name.
        KeychainError: The item is still stored and would not delete.
    """
    spec = _server(mcp, msg.server_id)
    kind = _kind_for(msg.server_id, spec, msg.target)
    mapping = _mapping(spec, kind)
    name = _find(mapping, kind, msg.name)
    if name is None:
        raise ConfigError(f"MCP server {msg.server_id!r} has no {label(kind, msg.name)}")
    entry = keychain_entry(mapping.pop(name))
    if entry is not None:
        await _delete_item(msg.server_id, entry)
    save_mcp_server(msg.server_id, _with(spec, kind, mapping), path)


async def _delete_item(server_id: str, entry: str) -> None:
    try:
        await keychain_mcp.delete_mcp_secret(server_id, entry)
    except KeychainError:
        # Already gone is what was asked for.
        if await keychain_mcp.mcp_secret_is_stored(server_id, entry):
            raise


async def _forget_item(server_id: str, entry: str) -> None:
    """Best effort: an item nothing names is deleted, or logged if it stays."""
    try:
        await _delete_item(server_id, entry)
    except KeychainError as e:
        log.warning(
            "mcp keychain item left behind",
            extra={"extra_fields": {"server_id": server_id, "entry": entry, "error": str(e)}},
        )


async def forget_server_secrets(server_id: str, spec: McpServerConfig) -> None:
    """Best effort after a server is removed: its items are now unreachable."""
    for value in (*spec.env.values(), *spec.headers.values()):
        entry = keychain_entry(value)
        if entry is not None:
            await _forget_item(server_id, entry)


async def value_summaries(
    server_id: str, mapping: Mapping[str, str], probe: StoredProbe
) -> list[McpValueSummary]:
    """Names, sources and presence; never a value, literal or not."""
    rows: list[McpValueSummary] = []
    for name, value in mapping.items():
        entry = keychain_entry(value)
        if entry is None:
            rows.append(McpValueSummary(name=name, source="literal"))
        else:
            stored = await probe(server_id, entry)
            rows.append(McpValueSummary(name=name, source="keychain", entry=entry, stored=stored))
    return rows
