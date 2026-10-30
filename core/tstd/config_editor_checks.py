"""What a Settings → Configuration edit must satisfy before it is written (TD-5203).

Three kinds of check, all run before anything touches the file: the value
has the shape its key takes (the model judges bounds and the rest), it
holds no key, token or URL password (directive 2: those live in the OS
keychain), and the loaded would-be config passes the checks the dedicated
Settings writers make beyond loading: an MCP server's off-box URL and
address, the phone listener's bind, a tier's window against its reply
room, an active model that exists, and a key a feature names. A failure
is :class:`ConfigEditRefused`, whose message is for the person and never
quotes the value.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from typing import Any

from .config import (
    DEFAULT_CREDENTIAL_ID,
    TIER_NAMES,
    ConfigError,
    ModelConfig,
    TierConfig,
)
from .config_editor_wire import ConfigNode
from .config_help import CHOICES_FROM
from .config_schema import editor_defs
from .config_write import _assert_mcp_http_url
from .config_yaml_emit import PathPart
from .logging import REDACTED, redact_secrets
from .mcp_values import url_host
from .protocol import ConfigRejectCode
from .tailscale_bind import resolve_remote_bind

_MAX_DEPTH = 12

#: A URL's scheme and authority: everything before its path, query or fragment.
_URL_AUTHORITY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^/?#\s\"'<>]*)")
#: A user name this long in a URL is a token, not a name.
_TOKEN_CHARS = 8
_WEB_SCHEMES = frozenset({"http", "https"})

_SECRET_SHAPED = (
    "{where} looks like it holds a key, a token or a password. Keep API keys in Settings → "
    "API keys and MCP tokens in Settings → MCP servers: both store them in the OS keychain, "
    "never in config.yaml."
)
_MCP_MOVED = (
    "This server's tokens and off-box opt-in were for its old address or transport: change "
    "those in Settings → MCP servers, which asks for the tokens again, or in config.yaml by hand."
)


def _userinfo_mask(scheme: str, userinfo: str) -> str | None:
    """How a URL's user info shows, or None when it shows as it is.

    A password is always hidden; so is a user name long enough to be a
    token (some services take one there), and any user on a web URL.
    """
    user, colon, _ = userinfo.partition(":")
    if colon and len(user) < _TOKEN_CHARS:
        return f"{user}:{REDACTED}"
    if colon or len(userinfo) >= _TOKEN_CHARS or scheme.lower() in _WEB_SCHEMES:
        return REDACTED
    return None


def _masked_url(match: re.Match[str]) -> str:
    scheme, authority = match.group(1), match.group(2)
    # The last "@" ends the user info: a raw "@" in a password comes before it.
    userinfo, at, host = authority.rpartition("@")
    mask = _userinfo_mask(scheme, userinfo) if at else None
    return match.group(0) if mask is None else f"{scheme}://{mask}@{host}"


def mask_url_userinfo(text: str) -> str:
    """*text* with the user info of every URL in it masked as :func:`_userinfo_mask` says."""
    return _URL_AUTHORITY.sub(_masked_url, text)


def scrub(text: str) -> str:
    """*text* fit for the window: URL user info masked, key shapes redacted."""
    return redact_secrets(mask_url_userinfo(text))


class ConfigEditRefused(Exception):
    """An edit that was not written. ``message`` is for the person, and never quotes the value.

    The message is scrubbed here as well: a validator's words can quote what
    was typed, and a typed URL can carry a password.
    """

    def __init__(self, code: ConfigRejectCode, message: str) -> None:
        message = scrub(message)
        super().__init__(message)
        self.code: ConfigRejectCode = code
        self.message = message


def dotted(path: list[PathPart]) -> str:
    """``presets.mine.brain.fallbacks[0]``: how the docs name a key."""
    out = ""
    for i, part in enumerate(path):
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if i else part)
    return out


def fields_of(node: ConfigNode) -> dict[str, ConfigNode]:
    """An object node's keys and their nodes, from the model it names."""
    assert node.ref is not None
    return {f.name: f.node for f in editor_defs()[node.ref].fields}


def check_writable(value: Any, where: str) -> None:
    """*value* can go into config.yaml: every character saves, and no secret is in it.

    A lone surrogate (half an emoji pair) would be written as an escape and
    then fail every reply that carries it. A key shape, a token the daemon
    knows, or a URL with user info belongs in the keychain (directive 2).
    """
    text = json.dumps(value, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigEditRefused(
            "invalid", f"{where} contains a character that cannot be saved"
        ) from None
    if scrub(text) != text:
        raise ConfigEditRefused("secret", _SECRET_SHAPED.format(where=where))


def check_value(node: ConfigNode, value: Any, where: str, depth: int = 0) -> None:
    """The value has the shape the key takes; bounds are left to the model."""
    if depth > _MAX_DEPTH:
        raise ConfigEditRefused("invalid", f"{where} is nested too deeply")
    _refuse_hidden(value, where)
    if value is None:
        if not node.nullable:
            raise ConfigEditRefused("invalid", f"{where} needs a value")
        return
    if not _shape_ok(node, value, where, depth):
        raise ConfigEditRefused("invalid", f"{where} must be {_shape_name(node)}")


def _shape_ok(node: ConfigNode, value: Any, where: str, depth: int) -> bool:
    kind = node.kind
    number = isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return number and math.isfinite(value)
    if kind == "str":
        return isinstance(value, str)
    if kind == "enum":
        return value in node.choices
    if kind == "secrets":
        raise ConfigEditRefused("secret", node.note)
    if kind == "json":
        _check_json(value, where, depth)
        return True
    if kind == "union":
        return any(_variant_ok(v, value, where, depth) for v in node.variants)
    if kind == "list" and node.items is not None:
        if not isinstance(value, list):
            return False
        for i, item in enumerate(value):
            check_value(node.items, item, f"{where}[{i}]", depth + 1)
        return True
    if kind == "map" and node.entries is not None:
        if not isinstance(value, dict):
            return False
        for key, item in value.items():
            check_value(node.entries, item, f"{where}.{key}", depth + 1)
        return True
    if kind == "object" and isinstance(value, dict):
        fields = fields_of(node)
        for key, item in value.items():
            if key not in fields:
                raise ConfigEditRefused("invalid", f"{where} has no key {key}")
            check_value(fields[key], item, f"{where}.{key}", depth + 1)
        return True
    return False


def _variant_ok(variant: ConfigNode, value: Any, where: str, depth: int) -> bool:
    try:
        return _shape_ok(variant, value, where, depth)
    except ConfigEditRefused:
        return False


def _refuse_hidden(value: Any, where: str) -> None:
    """A value the window showed masked must never be written back as the mask."""
    if isinstance(value, str) and REDACTED in value:
        raise ConfigEditRefused(
            "invalid",
            f"{where} still holds a hidden value ({REDACTED}). Type the real value, "
            "or keep a secret in the keychain.",
        )


def _check_json(value: Any, where: str, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ConfigEditRefused("invalid", f"{where} is nested too deeply")
    _refuse_hidden(value, where)
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigEditRefused("invalid", f"{where} must hold finite numbers")
    children: list[Any] = []
    if isinstance(value, dict):
        children = list(value.values())
    elif isinstance(value, list):
        children = value
    for child in children:
        _check_json(child, where, depth + 1)


def _shape_name(node: ConfigNode) -> str:
    if node.kind == "enum":
        return "one of " + ", ".join(str(c) for c in node.choices)
    return {
        "bool": "on or off",
        "int": "a whole number",
        "float": "a number",
        "str": "text",
        "list": "a list",
        "map": "a set of named entries",
        "object": "a group of keys",
        "union": "text or a list of words",
    }.get(node.kind, "a value")


def check_guards(
    config: ModelConfig,
    raw: dict[str, Any],
    path: list[PathPart],
    before: dict[str, Any],
) -> None:
    """The checks the dedicated Settings writers make beyond loading.

    *raw* is the would-be file and *config* what it loads as; *before* is
    the file as it is now.
    """
    if path == ["active_preset"] and raw.get("active_preset") not in (*config.presets, "", None):
        raise ConfigEditRefused("invalid", "there is no model by that name")
    if path[:2] == ["mcp", "servers"] and len(path) > 2 and isinstance(path[2], str):
        server = config.mcp.servers.get(path[2])
        if server is not None and server.transport == "http" and server.url:
            try:
                _assert_mcp_http_url(server.url, allow_remote=server.allow_remote)
            except ConfigError as e:
                raise ConfigEditRefused("invalid", str(e)) from e
        _check_mcp_address(config, before, path[2])
    _check_key_name(config, path)
    if path == ["remote", "bind"]:
        _check_remote_bind(config.remote.bind)
    if path[-1] in ("context_window", "max_output_tokens") and path[0] == "presets":
        tier = _tier_at(config, path[:-1])
        if tier is not None and tier.context_window <= tier.max_output_tokens * 2:
            raise ConfigEditRefused(
                "invalid",
                f"A {tier.context_window}-token window leaves too little room: this role "
                f"keeps {tier.max_output_tokens} tokens for its reply. Use more than "
                f"{tier.max_output_tokens * 2}.",
            )


def _check_mcp_address(config: ModelConfig, before: dict[str, Any], server_id: str) -> None:
    """A server moved to another host or transport keeps no token (TD-5161).

    Settings → MCP servers drops the off-box opt-in, env and headers, and
    forgets the keychain values, when either changes: they were for the old
    server. A token kept here would be sent to the new host on the next
    reload, so such a move is that page's to make.
    """
    mcp = before.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    old = servers.get(server_id) if isinstance(servers, dict) else None
    new = config.mcp.servers.get(server_id)
    if not isinstance(old, dict) or new is None:
        return
    old_url = old.get("url")
    moved = old.get("transport") != new.transport or url_host(
        old_url if isinstance(old_url, str) else None
    ) != url_host(new.url)
    holds = bool(old.get("env")) or bool(old.get("headers"))
    if moved and (holds or old.get("allow_remote") not in (None, False)):
        raise ConfigEditRefused("not_editable", _MCP_MOVED)


def _check_key_name(config: ModelConfig, path: list[PathPart]) -> None:
    """A section's key name (``voice.credential``…) names a key Settings has.

    A tier's credential is checked by the model itself; these were not, and
    the feature sends the item it names as a bearer token. The packaged
    name stays allowed, so Reset still works before that key is added.
    """
    if len(path) != 2 or not all(isinstance(p, str) for p in path):
        return
    section = getattr(config, str(path[0]), None)
    field = str(path[1])
    if section is None or CHOICES_FROM.get(f"{type(section).__name__}.{field}") != ("credentials",):
        return
    name = getattr(section, field)
    known = {*config.credentials, DEFAULT_CREDENTIAL_ID}
    if name and name not in known and name != type(section).model_fields[field].default:
        raise ConfigEditRefused(
            "invalid",
            f"{dotted(path)}: there is no saved key by that name; add it in Settings → API keys",
        )


def _tier_at(config: ModelConfig, path: list[PathPart]) -> TierConfig | None:
    if len(path) < 3 or not isinstance(path[1], str) or path[2] not in TIER_NAMES:
        return None
    preset = config.presets.get(path[1])
    if preset is None:
        return None
    tier: TierConfig = getattr(preset, str(path[2]))
    if path[3:4] == ["fallbacks"] and len(path) == 5 and isinstance(path[4], int):
        return tier.fallbacks[path[4]] if path[4] < len(tier.fallbacks) else None
    return tier if len(path) == 3 else None


def _check_remote_bind(spec: str) -> None:
    """Never a bind the listener would refuse; a name is checked when it binds."""
    spec = spec.strip()
    if not spec:
        return
    try:
        ipaddress.ip_address(spec.split("%", 1)[0])
    except ValueError:
        if spec != "*":
            return
    try:
        resolve_remote_bind(spec, lambda: {})
    except ValueError as e:
        raise ConfigEditRefused("invalid", str(e)) from e
