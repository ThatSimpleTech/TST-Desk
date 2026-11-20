"""Read MCP servers out of pasted JSON (TD-5253). Pure: no I/O, nothing run.

A server's README usually gives a block of JSON for some other client:
``{"mcpServers": {"name": {"command": "npx", "args": [...], "env": {...}}}}``.
This turns such a block into what Settings would have made by hand: an id,
an argv or a URL, and for each value where it will live. A token goes to
the keychain; ``config.yaml`` gets only a reference.

The text is hostile input. It is size- and count-capped, parsed as data
and never evaluated, and no error or preview here ever quotes a value
from ``env`` or ``headers``. Anything that would run an extra command or
read a file (``headersHelper``, ``envFile``) or that TST Desk cannot do
(OAuth, SSE, WebSocket) is refused or left out, and said so.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .mcp_values import check_name, check_secret
from .net import is_loopback_url

MAX_TEXT_BYTES = 256 * 1024
MAX_SERVERS = 50
MAX_ARGS = 200

Disposition = Literal["keychain", "literal", "needs_input"]

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PLACEHOLDER = "${"
_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE", re.IGNORECASE)
# Keys that make a client run something else or sign in; the server is not imported.
_REFUSED_KEYS = {
    "headersHelper": "it names a command to run for its headers",
    "oauth": "it needs an OAuth sign-in, which TST Desk does not do for MCP servers",
    "auth": "it needs an OAuth sign-in, which TST Desk does not do for MCP servers",
    "oauthClientId": "it needs an OAuth sign-in, which TST Desk does not do for MCP servers",
}
_KNOWN_KEYS = {"command", "args", "env", "url", "headers", "type", *_REFUSED_KEYS}
_HTTP_TYPES = {"http", "streamable-http", "streamablehttp"}


class McpImportError(ValueError):
    """The pasted text cannot be read at all. The message never quotes it."""


@dataclass(frozen=True)
class ImportValue:
    """One env var or header of a server to import. ``value`` never leaves the daemon."""

    name: str
    target: Literal["env", "header"]
    disposition: Disposition
    reason: str = ""
    value: str = field(default="", repr=False)


@dataclass(frozen=True)
class ImportServer:
    """One server read from the text."""

    source_name: str
    id: str
    transport: Literal["stdio", "http"] = "stdio"
    command: tuple[str, ...] = ()
    url: str = ""
    off_box: bool = False
    values: tuple[ImportValue, ...] = ()
    not_imported: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: Why this server cannot be imported; empty when it can.
    refused: str = ""


def strip_json_comments(text: str) -> str:
    """*text* without ``//`` and ``/* */`` comments and trailing commas.

    Editors' config files allow both and READMEs copy them. Strings are
    left alone, so a ``//`` inside a URL survives.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == ",":
            j = i + 1
            while j < n and (text[j] in " \t\r\n"):
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # a trailing comma: drop it
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def slug_id(name: str) -> str:
    """A server id from whatever the snippet called it: ``My Server_1`` → ``my-server-1``."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"mcp-{slug}".rstrip("-")
    return slug[:32].rstrip("-") or "mcp"


def valid_id(server_id: str) -> bool:
    return bool(_ID_RE.match(server_id))


def parse_import(text: str) -> list[ImportServer]:
    """Every server in *text*. McpImportError when it is not readable JSON of a known shape."""
    if len(text.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
        raise McpImportError(f"That is more than {MAX_TEXT_BYTES // 1024} KiB of text.")
    try:
        data = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        # The position, never the text: it may hold a token.
        raise McpImportError(f"That is not JSON (line {exc.lineno}, column {exc.colno}).") from None
    except RecursionError:
        raise McpImportError("That JSON is nested too deeply.") from None
    if not isinstance(data, dict):
        raise McpImportError("Paste the JSON object that lists the servers.")
    entries = _entries(data)
    if not entries:
        raise McpImportError("No servers found. Expected an mcpServers or servers block.")
    if len(entries) > MAX_SERVERS:
        raise McpImportError(f"That lists more than {MAX_SERVERS} servers.")
    servers: list[ImportServer] = []
    used: set[str] = set()
    for name, raw in entries:
        server = _server(name, raw)
        # Two names that slug alike must not land on one id.
        base, n = server.id, 1
        candidate = base
        while candidate in used:
            n += 1
            suffix = f"-{n}"
            candidate = f"{base[: 32 - len(suffix)]}{suffix}"
        used.add(candidate)
        if candidate != server.id:
            server = _replace(server, id=candidate)
        servers.append(server)
    return servers


def _replace(server: ImportServer, **changes: Any) -> ImportServer:
    return ImportServer(**{**server.__dict__, **changes})


def _entries(data: dict[str, Any]) -> list[tuple[str, Any]]:
    for wrapper in ("mcpServers", "servers"):
        block = data.get(wrapper)
        if isinstance(block, dict):
            return [(str(name), raw) for name, raw in block.items()]
    # One bare entry, as `claude mcp add-json` takes it.
    if "command" in data or "url" in data:
        return [("server", data)]
    return []


def _server(name: str, raw: Any) -> ImportServer:
    base = ImportServer(source_name=name, id=slug_id(name))
    if not isinstance(raw, dict):
        return _replace(base, refused="its entry is not a JSON object")
    for key, why in _REFUSED_KEYS.items():
        if key in raw:
            return _replace(base, refused=why)
    kind = raw.get("type")
    if kind is not None and not isinstance(kind, str):
        return _replace(base, refused="its type is not a word")
    kind = (kind or "").lower()
    if kind == "sse":
        return _replace(
            base, refused="it uses the older SSE transport, which TST Desk does not speak"
        )
    if kind in ("ws", "websocket"):
        return _replace(
            base, refused="it uses a WebSocket transport, which TST Desk does not speak"
        )
    not_imported = tuple(sorted(str(k) for k in raw if k not in _KNOWN_KEYS))
    notes: list[str] = []
    if "sandbox" in raw or "sandboxEnabled" in raw:
        notes.append("The snippet expects a sandbox. TST Desk will not put this server in one.")
    url = raw.get("url")
    if url is not None or kind in _HTTP_TYPES:
        if not isinstance(url, str) or not url.strip():
            return _replace(base, refused="it has no url")
        if kind and kind not in _HTTP_TYPES:
            return _replace(base, refused=f"its type {kind!r} is not one TST Desk knows")
        if _PLACEHOLDER in url:
            return _replace(
                base, refused="its url has a ${...} placeholder; put the real value in first"
            )
        if not kind:
            notes.append("No type was given; assumed Streamable HTTP.")
        off_box = not is_loopback_url(url)
        if off_box and not url.lower().startswith("https://"):
            return _replace(base, refused="a server on another machine must use https")
        values, refused = _values("header", raw.get("headers"))
        if refused:
            return _replace(base, refused=refused)
        if raw.get("env"):
            not_imported = (*not_imported, "env")
        return _replace(
            base,
            transport="http",
            url=url.strip(),
            off_box=off_box,
            values=values,
            not_imported=not_imported,
            notes=tuple(notes),
        )
    if kind and kind != "stdio":
        return _replace(base, refused=f"its type {kind!r} is not one TST Desk knows")
    command = raw.get("command")
    args = raw.get("args", [])
    if not isinstance(command, str) or not command.strip():
        return _replace(base, refused="it has no command")
    if not isinstance(args, list) or not all(isinstance(a, str | int | float) for a in args):
        return _replace(base, refused="its args are not a list of words")
    if any(isinstance(a, bool) for a in args) or len(args) > MAX_ARGS:
        return _replace(base, refused="its args are not a list of words")
    argv = (command.strip(), *(str(a) for a in args))
    if any(_PLACEHOLDER in token for token in argv):
        return _replace(
            base, refused="its command has a ${...} placeholder; put the real value in first"
        )
    if " " in command.strip() and not args:
        notes.append("The command has spaces and no args. It is run as one word, not split.")
    if argv[0].rsplit("/", 1)[-1].lower() in {
        "sh",
        "bash",
        "zsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
    }:
        notes.append("This runs a shell. Read the whole command before importing it.")
    values, refused = _values("env", raw.get("env"))
    if refused:
        return _replace(base, refused=refused)
    if raw.get("headers"):
        not_imported = (*not_imported, "headers")
    return _replace(
        base, command=argv, values=values, not_imported=not_imported, notes=tuple(notes)
    )


def _values(target: Literal["env", "header"], raw: Any) -> tuple[tuple[ImportValue, ...], str]:
    """The values, or a reason the server is refused. Never quotes a value."""
    if raw is None:
        return (), ""
    if not isinstance(raw, dict):
        return (), f"its {'env' if target == 'env' else 'headers'} is not a JSON object"
    out: list[ImportValue] = []
    for key, value in raw.items():
        name = str(key)
        try:
            check_name(target, name)
        except ValueError as exc:
            return (), str(exc)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            return (), f"the value of {name} is not text"
        out.append(_classify(target, name, str(value)))
    return tuple(out), ""


def _classify(target: Literal["env", "header"], name: str, value: str) -> ImportValue:
    if _PLACEHOLDER in value:
        return ImportValue(
            name, target, "needs_input", "The snippet reads this from a variable. Type the value."
        )
    problem = check_secret(value)
    if problem is None:
        return ImportValue(name, target, "keychain", value=value)
    if target == "header" or _SECRET_NAME.search(name):
        return ImportValue(
            name, target, "needs_input", f"The value given {problem}. Type the real one."
        )
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        return ImportValue(name, target, "needs_input", "The value must be one line.")
    # Short, and not named like a secret: a port, a flag, a mode.
    return ImportValue(name, target, "literal", value=value)
