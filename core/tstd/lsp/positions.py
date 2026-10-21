"""Positions and URIs between the model, the file and a language server (TD-5139).

The model counts lines and columns from 1 in code points; LSP counts from
0 in the encoding the server negotiated (UTF-16 unless it picks UTF-8).
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


def to_server_column(text: str, column: int, encoding: str) -> int:
    """A 0-based code-point *column* in line *text* in the server's units."""
    head = text[:column]
    if encoding == "utf-8":
        return len(head.encode("utf-8"))
    if encoding == "utf-32":
        return len(head)
    return len(head.encode("utf-16-le")) // 2


def from_server_column(text: str, column: int, encoding: str) -> int:
    """The server's 0-based *column* in line *text* as a code-point index."""
    if encoding == "utf-32":
        return min(column, len(text))
    units = 0
    for i, ch in enumerate(text):
        if units >= column:
            return i
        units += len(ch.encode("utf-8")) if encoding == "utf-8" else (2 if ord(ch) > 0xFFFF else 1)
    return len(text)


def find_column(text: str, symbol: str) -> int | None:
    """Where *symbol* starts on line *text*: a whole word first, else any match."""
    symbol = symbol.strip()
    if not symbol:
        return None
    word = re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", text)
    if word is not None:
        return word.start()
    at = text.find(symbol)
    return at if at >= 0 else None


def uri_path(uri: str) -> Path | None:
    """The local path a ``file:`` URI names, or None for any other scheme."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(unquote(parsed.path)))
