"""Save servers read from pasted JSON (TD-5253): keychain first, then config.

The window's preview is not trusted. The same text is parsed again here
and every choice the person made (an id, a typed value, the off-box tick)
is checked against that parse. A server is saved whole or not at all: its
secrets are stored first, and if the config write then fails they are
deleted again, so no keychain item is left that nothing names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import keychain_mcp
from .config import ConfigError, McpConfig, McpServerConfig
from .config_write import save_mcp_server
from .logging import get_logger
from .mcp_import import ImportServer, McpImportError, parse_import, valid_id
from .mcp_values import check_secret, entry_for_name, keychain_ref

log = get_logger("tstd.mcp_import")


@dataclass(frozen=True)
class Choice:
    """What the person decided for one server in the preview."""

    source_name: str
    id: str
    allow_remote: bool = False
    inputs: Mapping[str, str] | None = None


@dataclass(frozen=True)
class _Ready:
    server: ImportServer
    id: str
    spec: McpServerConfig
    secrets: dict[str, str]  # keychain entry -> value


def _ready(server: ImportServer, choice: Choice, mcp: McpConfig, taken: set[str]) -> _Ready:
    where = f"{server.source_name!r}"
    if server.refused:
        raise ConfigError(f"{where} cannot be imported: {server.refused}.")
    if not valid_id(choice.id):
        raise ConfigError(
            f"{where}: the id must be a lowercase slug, letters, digits and -, at most 32."
        )
    if choice.id in mcp.servers or choice.id in taken:
        raise ConfigError(
            f"{where}: a server with the id {choice.id!r} is already listed. "
            "Give this one another id, or remove that one first."
        )
    if server.off_box and not choice.allow_remote:
        raise ConfigError(
            f"{where} is on another machine. Tick that you allow connecting to it to import it."
        )
    inputs = dict(choice.inputs or {})
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for value in server.values:
        mapping = env if value.target == "env" else headers
        if value.disposition == "literal":
            mapping[value.name] = value.value
            continue
        text = value.value
        if value.disposition == "needs_input":
            text = inputs.get(value.name, "")
            if not text:
                raise ConfigError(f"{where}: type a value for {value.name}.")
        problem = check_secret(text)
        if problem is not None:
            raise ConfigError(f"{where}: the value for {value.name} {problem}.")
        base = entry_for_name(value.name)
        entry, n = base, 1
        while entry in secrets:
            n += 1
            suffix = f"-{n}"
            entry = f"{base[: 64 - len(suffix)]}{suffix}"
        secrets[entry] = text
        mapping[value.name] = keychain_ref(entry)
    try:
        spec = McpServerConfig(
            transport=server.transport,
            command=list(server.command),
            url=server.url,
            enabled=True,
            allow_remote=server.off_box and choice.allow_remote,
            env=env,
            headers=headers,
        )
    except ValueError as exc:
        # A pydantic error can carry its input; give the reasons only.
        reasons = getattr(exc, "errors", None)
        detail = (
            "; ".join(str(e.get("msg", "")) for e in reasons(include_input=False))
            if callable(reasons)
            else "its settings are not valid"
        )
        raise ConfigError(f"{where}: {detail}") from None
    return _Ready(server, choice.id, spec, secrets)


async def apply_import(
    text: str, choices: list[Choice], mcp: McpConfig, path: Path
) -> list[tuple[str, ImportServer]]:
    """Import the chosen servers. Returns ``(id, server)`` for each one saved.

    Every choice is checked before anything is written, so a mistake in
    the third server does not leave the first two half-done.

    Raises:
        ConfigError: The text, a choice or a value is not acceptable. Never quotes a value.
        KeychainError: The keychain refused a value.
    """
    try:
        parsed = {server.source_name: server for server in parse_import(text)}
    except McpImportError as exc:
        raise ConfigError(str(exc)) from None
    if not choices:
        raise ConfigError("Choose at least one server to import.")
    ready: list[_Ready] = []
    taken: set[str] = set()
    for choice in choices:
        server = parsed.get(choice.source_name)
        if server is None:
            raise ConfigError(f"There is no server named {choice.source_name!r} in that text.")
        item = _ready(server, choice, mcp, taken)
        taken.add(item.id)
        ready.append(item)
    saved: list[tuple[str, ImportServer]] = []
    for item in ready:
        stored: list[str] = []
        try:
            for entry, value in item.secrets.items():
                await keychain_mcp.store_mcp_secret(item.id, entry, value)
                stored.append(entry)
            save_mcp_server(item.id, item.spec, path)
        except Exception:
            for entry in stored:
                try:
                    await keychain_mcp.delete_mcp_secret(item.id, entry)
                except Exception:
                    log.exception(
                        "could not remove an imported value after a failed save",
                        extra={"extra_fields": {"server_id": item.id, "entry": entry}},
                    )
            raise
        saved.append((item.id, item.server))
    return saved
