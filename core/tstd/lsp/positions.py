"""Positions and URIs between the model, the file and a language server (TD-5139).

The model counts lines and columns from 1 in code points; LSP counts from
0 in the encoding the server negotiated (UTF-16 unless it picks UTF-8).
"""

from __future__ import annotations

import ntpath
import os
import re
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlparse

_DRIVE_PATH = re.compile(r"^/[A-Za-z]:")


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


def _uri_path_text(uri: str, pathmod: ModuleType) -> str | None:
    """The path a ``file:`` URI names, as text for *pathmod*'s platform.

    Written out by hand rather than with ``urllib.request.url2pathname``,
    which would give this module a network-capable import (§2.3). Every
    escape is decoded (``%3A`` too, which servers built on vscode-uri send
    for a drive colon), ``/c:/x`` loses its leading slash, and on Windows
    a host other than ``localhost`` becomes a UNC path.
    """
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "file":
        return None
    path = unquote(parsed.path)
    host = parsed.netloc if parsed.netloc.lower() != "localhost" else ""
    if pathmod is ntpath:
        if _DRIVE_PATH.match(path):
            path = path[1:]
        elif host:
            path = f"//{host}{path}"
        return path.replace("/", "\\")
    return path


def uri_path(uri: str) -> Path | None:
    """The local path a ``file:`` URI names, or None for any other scheme."""
    text = _uri_path_text(uri, os.path)
    return Path(text) if text is not None else None


def uri_key(uri: str, pathmod: ModuleType = os.path) -> str:
    """One spelling for every URI that names the same local file.

    Servers do not echo our ``Path.as_uri()`` byte for byte: on Windows
    many send ``file:///c%3A/...`` for ``file:///C:/...``. Diagnostics are
    filed and looked up under this key, so either spelling matches. A URI
    that is not ``file:`` is its own key.
    """
    text = _uri_path_text(uri, pathmod)
    if not text:
        return uri
    return str(pathmod.normcase(pathmod.normpath(text)))
