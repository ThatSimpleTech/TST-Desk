"""Env vars and HTTP headers for one listed MCP server (TD-5110).

Each value in ``mcp.servers.<id>.env`` or ``.headers`` is either a literal
(not a secret: it sits in ``config.yaml``) or ``keychain:<entry>``. A
reference names the OS keychain item ``tst-mcp.<server id>.<entry>``, so a
server can only reach its own items. The daemon reads them when it starts
that server and hands them to that child or its requests alone.

Pure: no I/O and no daemon imports, so ``config.py`` can validate with it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

ValueKind = Literal["env", "header"]

KEYCHAIN_PREFIX = "keychain:"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
#: An HTTP field name is a token (RFC 9110 §5.1).
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
#: Lowercase so two spellings never share an item on Windows, whose
#: Credential Manager matches target names without regard to case.
ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Set by the client itself, or they would change how the request is read.
RESERVED_HEADERS = frozenset(
    {"accept", "connection", "content-length", "content-type", "host", "transfer-encoding"}
)
#: MCP's own headers are the client's too (TD-5136): a configured one
#: could contradict the request body. Only the names the spec reserves
#: (TD-5161); a server's own ``Mcp-Api-Key`` style header is allowed.
PROTOCOL_HEADERS = frozenset({"mcp-protocol-version", "mcp-session-id", "mcp-method", "mcp-name"})
#: ``Mcp-Param-<name>`` carries a tool parameter the server marks.
PROTOCOL_HEADER_PREFIX = "mcp-param-"

#: Shortest secret the daemon accepts. The logs, the audit DB and the wire
#: scrub every secret it has read by its exact text, and a shorter string
#: would also match ordinary words.
MIN_SECRET_CHARS = 8

_FORBIDDEN_CHARS = ("\r", "\n", "\x00")


def keychain_entry(value: str) -> str | None:
    """The entry a ``keychain:<entry>`` value names, or None for a literal."""
    if not value.startswith(KEYCHAIN_PREFIX):
        return None
    return value[len(KEYCHAIN_PREFIX) :]


def keychain_ref(entry: str) -> str:
    return f"{KEYCHAIN_PREFIX}{entry}"


def entry_for_name(name: str) -> str:
    """The entry Settings files a value under: the name, lowercased and slugged.

    ``GITHUB_TOKEN`` becomes ``github_token`` and ``Authorization`` becomes
    ``authorization``.
    """
    slug = re.sub(r"[^a-z0-9_-]", "-", name.lower()).lstrip("_-")[:64]
    return slug or "secret"


def label(kind: ValueKind, name: str) -> str:
    return f"env {name}" if kind == "env" else f"header {name}"


def check_name(kind: ValueKind, name: str) -> None:
    if kind == "env":
        if not ENV_NAME_RE.match(name):
            raise ValueError(
                f"env name {name!r} must be letters, digits and _, not starting with a digit"
            )
        return
    if not HEADER_NAME_RE.match(name):
        raise ValueError(f"header name {name!r} is not a valid HTTP field name")
    folded = name.lower()
    if (
        folded in RESERVED_HEADERS
        or folded in PROTOCOL_HEADERS
        or folded.startswith(PROTOCOL_HEADER_PREFIX)
    ):
        raise ValueError(f"header {name!r} is set by TST Desk and cannot be configured")


def check_value(kind: ValueKind, name: str, value: str) -> None:
    """A literal or a reference as config holds it. Never echoes the value."""
    if any(ch in value for ch in _FORBIDDEN_CHARS):
        raise ValueError(f"{label(kind, name)} must be one line with no NUL")
    entry = keychain_entry(value)
    if entry is not None and not ENTRY_RE.match(entry):
        raise ValueError(
            f"{label(kind, name)}: keychain entry {entry!r} must be lowercase "
            "letters, digits, _ and -, starting with a letter or digit (at most 64)"
        )


def coerce_map(kind: ValueKind, raw: Any) -> dict[str, str]:
    """Validate one ``env`` or ``headers`` mapping from YAML or a message.

    Numbers become strings (``PORT: 8080``). A YAML boolean is refused,
    because ``true`` would reach the child as ``True``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{kind} must be a mapping of name to value")
    out: dict[str, str] = {}
    seen: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        check_name(kind, name)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValueError(f"{label(kind, name)} must be a string; quote it in config.yaml")
        text = str(value)
        check_value(kind, name, text)
        if kind == "header":
            folded = name.lower()
            if folded in seen:
                raise ValueError(f"header {name!r} is listed twice (as {seen[folded]!r})")
            seen[folded] = name
        out[name] = text
    return out


def check_secret(value: str) -> str | None:
    """Why *value* cannot be a stored secret, or None when it can.

    One line of printable ASCII: macOS ``security -w`` prints anything
    else as hex, and an HTTP header must not carry it either. The reason
    never includes the value.
    """
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        return "must be one line of printable ASCII"
    if len(value.strip()) < MIN_SECRET_CHARS:
        return f"must be at least {MIN_SECRET_CHARS} characters"
    # The scrub registers only parts this long, so a short token after a
    # scheme ("Bearer abc1234") would be hidden only as a whole (TD-5160).
    if len(value.split()[-1]) < MIN_SECRET_CHARS:
        return f"must end in a part of at least {MIN_SECRET_CHARS} characters (the token)"
    return None


def url_host(url: str | None) -> str | None:
    """An MCP url's host and port, lowercased; None for no url (stdio).

    Two urls with the same one reach the same server (TD-5161): its
    off-box opt-in and its tokens carry over. Any other host does not.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        return f"{(parts.hostname or '').lower()}:{parts.port}"
    except ValueError:
        return url
