"""Values written into config.yaml as YAML that reads back the same (TD-5203).

The splicer in :mod:`tstd.config_yaml_edit` decides where text goes; this
decides what the text is. Every new value is JSON, which YAML reads back as
the same value: a string is always double-quoted (so ``on``, ``1.0`` or
``2026-10-09`` stays a string), a float always has a decimal point (YAML 1.1
reads ``1e-05`` as a string), and a character YAML would fold or refuse is
escaped. Collections are written in block style, or flow style when asked.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from typing import Any

import yaml

PathPart = str | int

#: Characters YAML will not take raw inside a double-quoted scalar, or would
#: read as a line break and fold: escaped as ``\\uXXXX`` instead.
_UNSAFE_IN_QUOTES = re.compile(
    "[^\x09\x0a\x0d\x20-\x7e\xa0-\U0000d7ff\U0000e000-\U0000fffd\U00010000-\U0010ffff]"
    "|[\x85\U00002028\U00002029\U0000feff]"
)
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class YamlEditError(ValueError):
    """The edit cannot be made to this document's text."""


def quote(text: str) -> str:
    out = json.dumps(text, ensure_ascii=False)
    return _UNSAFE_IN_QUOTES.sub(lambda m: f"\\u{ord(m.group()):04x}", out)


def scalar_text(value: Any) -> str:
    """One JSON scalar as YAML that reads back as the same value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise YamlEditError("a number must be finite")
        text = repr(value)
        if "e" not in text:
            return text
        mantissa, exponent = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        if exponent[0] not in "+-":
            exponent = "+" + exponent
        return f"{mantissa}e{exponent}"
    if isinstance(value, str):
        return quote(value)
    raise YamlEditError(f"cannot write a {type(value).__name__} value")


def flow_text(value: Any) -> str:
    """*value* on one line, in YAML's JSON-like flow style."""
    if isinstance(value, dict):
        items = ", ".join(f"{quote(str(k))}: {flow_text(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(flow_text(v) for v in value) + "]"
    return scalar_text(value)


def _key_text(key: str) -> str:
    """A mapping key, plain when YAML reads it back as that same string."""
    if _PLAIN_KEY.match(key):
        try:
            back = yaml.safe_load(f"{key}: 1")
        except yaml.YAMLError:
            back = None
        if isinstance(back, dict) and list(back) == [key]:
            return key
    return quote(key)


def nonempty_collection(value: Any) -> bool:
    return isinstance(value, dict | list) and bool(value)


def entry_lines(key: str, value: Any, indent: int, flow: bool) -> list[str]:
    """``key: value`` at *indent*, nested values in block style unless *flow*."""
    pad = " " * indent
    if flow or not nonempty_collection(value):
        return [f"{pad}{_key_text(key)}: {flow_text(value)}"]
    head = f"{pad}{_key_text(key)}:"
    if isinstance(value, dict):
        return [head] + [
            line for k, v in value.items() for line in entry_lines(str(k), v, indent + 2, False)
        ]
    return [head, *item_lines(value, indent + 2)]


def item_lines(items: list[Any], indent: int) -> list[str]:
    """Block sequence items at *indent* (the column of each ``-``)."""
    pad = " " * indent
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict) and item:
            body = [
                line for k, v in item.items() for line in entry_lines(str(k), v, indent + 2, False)
            ]
            body[0] = pad + "- " + body[0][indent + 2 :]
            lines.extend(body)
        else:
            lines.append(f"{pad}- {flow_text(item)}")
    return lines


def nest(parts: Sequence[PathPart], value: Any) -> Any:
    """The value that holds *value* at *parts* below a missing key."""
    for part in reversed(parts):
        if isinstance(part, int):
            if part != 0:
                raise YamlEditError("a new list starts at item 0")
            value = [value]
        else:
            value = {part: value}
    return value
