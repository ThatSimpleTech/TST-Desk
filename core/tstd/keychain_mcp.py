"""Keychain items for MCP server env vars and headers (TD-5110).

One item per value, under ``tst-mcp.<server id>.<entry>``. The ``.``
keeps them out of the credential namespace (``tst-<id>``, whose ids allow
no ``.``), and the server id in the name means one server's config can
only ever reach its own items. Every value read or stored here is
registered with the log redactor, so its exact text is scrubbed from the
logs, the audit DB and the wire from then on.
"""

from __future__ import annotations

import re

from . import keychain as _keychain
from .keychain import KeychainError
from .logging import register_secret_value
from .mcp_values import ENTRY_RE, check_secret

ACCOUNT_PREFIX = "tst-mcp."
#: Same slug as ``mcp.servers`` ids (``config.CREDENTIAL_ID_RE``).
_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def mcp_secret_account(server_id: str, entry: str) -> str:
    """``tst-mcp.<server>.<entry>``. Refuses anything that is not a slug."""
    if not _SERVER_ID_RE.match(server_id):
        raise KeychainError(f"not an MCP server id: {server_id!r}")
    if not ENTRY_RE.match(entry):
        raise KeychainError(f"not a keychain entry name: {entry!r}")
    return f"{ACCOUNT_PREFIX}{server_id}.{entry}"


def _refuse(value: str, account: str) -> None:
    reason = check_secret(value)
    if reason is not None:
        raise KeychainError(f"the value for {account} {reason}")


async def store_mcp_secret(server_id: str, entry: str, value: str) -> None:
    """Store one value. The error never includes it.

    Raises:
        KeychainError: If the value is not one printable line of at least
            eight characters, or storage fails (KeychainLockedError when
            the keychain is locked).
    """
    account = mcp_secret_account(server_id, entry)
    _refuse(value, account)
    register_secret_value(value)
    await _keychain._get_backend().set_secret(account, value)


async def get_mcp_secret(server_id: str, entry: str) -> str:
    """Read one value for a server that is starting.

    Raises:
        KeychainError: If it is missing, unreadable, or not a value
            :func:`store_mcp_secret` would have accepted.
    """
    account = mcp_secret_account(server_id, entry)
    value = await _keychain._get_backend().get_secret(account)
    # Registered before the shape check: a bad value is still a secret.
    register_secret_value(value)
    _refuse(value, account)
    return value


async def delete_mcp_secret(server_id: str, entry: str) -> None:
    """Delete one value.

    Raises:
        KeychainError: If deletion fails.
    """
    await _keychain._get_backend().delete_secret(mcp_secret_account(server_id, entry))


async def mcp_secret_is_stored(server_id: str, entry: str) -> bool:
    """Whether the item exists. On macOS this never reads the value (TD-4838)."""
    return await _keychain._get_backend().has_secret(mcp_secret_account(server_id, entry))
