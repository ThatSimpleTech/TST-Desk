"""A tiny language server for the TD-5139 tests, spoken to over stdio.

It knows one toy language: ``def NAME`` defines a function, ``class NAME``
a class (an indented ``def`` under it is a method), any other whole-word
use of a name is a reference. A line holding ``BAD`` is an error and one
holding ``WARN`` a warning. Its first argument names a file it appends each
method it receives to, so a test can see what the client sent. With
``--respell-uris`` it publishes diagnostics under a differently escaped
URI for the same file (every ``.`` as ``%2E``), as servers built on
vscode-uri do with a Windows drive colon.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.parse import unquote, urlparse

docs: dict[str, str] = {}
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
log_path = _args[0] if _args else ""
respell = "--respell-uris" in sys.argv[1:]


def note(method: str) -> None:
    if log_path:
        with open(log_path, "a") as f:
            f.write(method + "\n")


def send(message: dict[str, Any]) -> None:
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def read() -> dict[str, Any] | None:
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode().partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def text_of(uri: str) -> str:
    if uri in docs:
        return docs[uri]
    with open(unquote(urlparse(uri).path)) as f:
        return f.read()


def word_at(uri: str, pos: dict[str, int]) -> str:
    line = text_of(uri).splitlines()[pos["line"]]
    for m in re.finditer(r"\w+", line):
        if m.start() <= pos["character"] <= m.end():
            return m.group()
    return ""


def rng(line: int, start: int, end: int) -> dict[str, Any]:
    return {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}


def publish(uri: str) -> None:
    diags = []
    for n, line in enumerate(docs[uri].splitlines()):
        for token, severity, code, message in (
            ("BAD", 1, "E1", "bad token"),
            ("WARN", 2, "W1", "suspicious token"),
        ):
            at = line.find(token)
            if at >= 0:
                diags.append(
                    {
                        "range": rng(n, at, at + len(token)),
                        "severity": severity,
                        "code": code,
                        "message": message,
                    }
                )
    send(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri.replace(".", "%2E") if respell else uri, "diagnostics": diags},
        }
    )


def definitions() -> list[tuple[str, int, int, str, str, bool]]:
    found = []
    for uri, text in docs.items():
        for n, line in enumerate(text.splitlines()):
            m = re.match(r"(\s*)(def|class) (\w+)", line)
            if m:
                found.append((uri, n, m.start(3), m.group(3), m.group(2), bool(m.group(1))))
    return found


def handle(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    params = msg.get("params") or {}
    rid = msg.get("id")
    if method is None:
        return  # a reply to our own request
    note(str(method))
    if method == "initialize":
        if any("API_KEY" in name for name in os.environ):
            note("saw-a-secret")
        send(
            {
                "id": rid,
                "result": {
                    "capabilities": {
                        "positionEncoding": "utf-16",
                        "textDocumentSync": 1,
                        "definitionProvider": True,
                        "referencesProvider": True,
                        "documentSymbolProvider": True,
                        "workspaceSymbolProvider": True,
                    }
                },
            }
        )
    elif method == "initialized":
        # Servers ask the client things; the client must answer.
        send({"id": "cfg", "method": "workspace/configuration", "params": {"items": [{}]}})
    elif method == "textDocument/didOpen":
        doc = params["textDocument"]
        docs[doc["uri"]] = doc["text"]
        publish(doc["uri"])
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        docs[uri] = params["contentChanges"][-1]["text"]
        publish(uri)
    elif method == "textDocument/definition":
        name = word_at(params["textDocument"]["uri"], params["position"])
        hits = [
            {"uri": u, "range": rng(n, c, c + len(name))}
            for u, n, c, d, _k, _i in definitions()
            if d == name
        ]
        send({"id": rid, "result": hits})
    elif method == "textDocument/references":
        name = word_at(params["textDocument"]["uri"], params["position"])
        hits = []
        for uri, text in docs.items():
            for n, line in enumerate(text.splitlines()):
                for m in re.finditer(rf"\b{re.escape(name)}\b", line):
                    hits.append({"uri": uri, "range": rng(n, m.start(), m.end())})
        send({"id": rid, "result": hits})
    elif method == "textDocument/documentSymbol":
        uri = params["textDocument"]["uri"]
        out: list[dict[str, Any]] = []
        for u, n, c, name, kind, indented in definitions():
            if u != uri:
                continue
            sym = {
                "name": name,
                "kind": 5 if kind == "class" else (6 if indented else 12),
                "range": rng(n, 0, c + len(name)),
                "selectionRange": rng(n, c, c + len(name)),
                "children": [],
            }
            if indented and out:
                out[-1]["children"].append(sym)
            else:
                out.append(sym)
        send({"id": rid, "result": out})
    elif method == "workspace/symbol":
        query = params["query"]
        send(
            {
                "id": rid,
                "result": [
                    {
                        "name": name,
                        "kind": 12,
                        "location": {"uri": u, "range": rng(n, c, c + len(name))},
                    }
                    for u, n, c, name, _k, _i in definitions()
                    if query in name
                ],
            }
        )
    elif method == "shutdown":
        send({"id": rid, "result": None})
    elif method == "exit":
        sys.exit(0)
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": "nope"}})


def main() -> None:
    while True:
        msg = read()
        if msg is None:
            return
        handle(msg)


if __name__ == "__main__":
    main()
