"""Resolve one server's env or headers just before it starts (TD-5110).

Literals pass through. ``keychain:<entry>`` is read from that server's own
keychain item at this moment, never earlier, and the result goes only to
the client being built for that server. A missing item fails that server
with a doctor row; it never starts with the value left out.
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import keychain_mcp
from ..config import McpServerConfig
from ..keychain import KeychainError, KeychainLockedError
from ..mcp_values import ValueKind, keychain_entry, label
from .errors import McpError


def _settings_fix(server_id: str, kind: ValueKind, name: str) -> str:
    return (
        f"Set {label(kind, name)} in Settings → MCP servers → {server_id}, "
        f"or remove it from mcp.servers.{server_id}.{'env' if kind == 'env' else 'headers'}."
    )


async def server_values(server_id: str, spec: McpServerConfig) -> dict[str, str]:
    """The env (stdio) or headers (http) this server starts with.

    A value the transport cannot deliver fails the server rather than
    being dropped without a word.

    Raises:
        McpError: Values on the wrong transport, or one that cannot be read.
    """
    if spec.transport == "stdio":
        if spec.headers:
            raise McpError(
                "headers only apply to http servers",
                fix=f"Remove mcp.servers.{server_id}.headers; a stdio server takes env.",
            )
        return await resolve_values(server_id, "env", spec.env)
    if spec.env:
        raise McpError(
            "env only applies to stdio servers",
            fix=f"Remove mcp.servers.{server_id}.env; an http server takes headers.",
        )
    return await resolve_values(server_id, "header", spec.headers)


async def resolve_values(
    server_id: str, kind: ValueKind, mapping: Mapping[str, str]
) -> dict[str, str]:
    """Literal values as written, keychain references read now.

    Raises:
        McpError: An item is missing, locked, or not a usable value. The
            message names the item, never its value.
    """
    resolved: dict[str, str] = {}
    for name, raw in mapping.items():
        entry = keychain_entry(raw)
        if entry is None:
            resolved[name] = raw
            continue
        try:
            resolved[name] = await keychain_mcp.get_mcp_secret(server_id, entry)
        except KeychainLockedError as e:
            raise McpError(
                f"{label(kind, name)} needs the keychain, which is locked",
                fix=str(e),
            ) from None
        except KeychainError as e:
            raise McpError(
                await _why_unread(server_id, kind, name, entry, e),
                fix=_settings_fix(server_id, kind, name),
            ) from None
    return resolved


async def _why_unread(
    server_id: str, kind: ValueKind, name: str, entry: str, error: KeychainError
) -> str:
    """Name the item and say whether it is missing; the CLI's own words otherwise."""
    account = f"{keychain_mcp.ACCOUNT_PREFIX}{server_id}.{entry}"
    try:
        stored = await keychain_mcp.mcp_secret_is_stored(server_id, entry)
    except KeychainError:
        stored = True
    if not stored:
        return f"{label(kind, name)} needs keychain item {account}, which is not stored"
    return f"{label(kind, name)} could not read keychain item {account}: {error}"
