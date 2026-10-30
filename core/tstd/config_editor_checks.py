"""What a Settings → Configuration edit must satisfy before it is written (TD-5203).

Two kinds of check, both run before anything touches the file: the value
has the shape its key takes (the model judges bounds and the rest), and the
loaded would-be config passes the checks the dedicated Settings writers
make beyond loading: an MCP server's off-box URL, the phone listener's
bind, a tier's window against its reply room, and an active model that
exists. A failure is :class:`ConfigEditRefused`, whose message is for the
person and never quotes the value.
"""

from __future__ import annotations

import ipaddress
import math
from typing import Any

from .config import TIER_NAMES, ConfigError, ModelConfig, TierConfig
from .config_editor_wire import ConfigNode
from .config_schema import editor_defs
from .config_write import _assert_mcp_http_url
from .config_yaml_emit import PathPart
from .logging import REDACTED
from .protocol import ConfigRejectCode
from .tailscale_bind import resolve_remote_bind

_MAX_DEPTH = 12


class ConfigEditRefused(Exception):
    """An edit that was not written. ``message`` is for the person, and never quotes the value."""

    def __init__(self, code: ConfigRejectCode, message: str) -> None:
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


def check_guards(config: ModelConfig, raw: dict[str, Any], path: list[PathPart]) -> None:
    """The checks the dedicated Settings writers make beyond loading."""
    if path == ["active_preset"] and raw.get("active_preset") not in (*config.presets, "", None):
        raise ConfigEditRefused("invalid", "there is no model by that name")
    if path[:2] == ["mcp", "servers"] and len(path) > 2 and isinstance(path[2], str):
        server = config.mcp.servers.get(path[2])
        if server is not None and server.transport == "http" and server.url:
            try:
                _assert_mcp_http_url(server.url, allow_remote=server.allow_remote)
            except ConfigError as e:
                raise ConfigEditRefused("invalid", str(e)) from e
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
