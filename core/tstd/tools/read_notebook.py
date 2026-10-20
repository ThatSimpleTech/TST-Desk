"""Jupyter notebook view for ``fs_read`` (TD-5130).

A notebook is JSON whose cell sources and outputs are lists of strings,
so read raw the code is hard to follow and a traceback is wrapped in
terminal colour codes. ``render_notebook`` turns an nbformat 4 notebook
into plain lines: each cell's source under a marker, then its text
outputs (streams, results, errors). Rich outputs with no text form are
named, not shown. Anything that is not nbformat 4 JSON returns ``None``
and ``fs_read`` reads the file as text, as it did before.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Text forms of a result, best first. HTML is left out: a table almost
#: always carries ``text/plain`` too, and raw markup reads badly.
_TEXT_MIMES = ("text/plain", "text/markdown", "text/latex", "application/json")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _text(value: object) -> str:
    """nbformat multiline strings are a string or a list of strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part for part in value if isinstance(part, str))
    return ""


def _lines(text: str) -> list[str]:
    return text.rstrip("\n").split("\n") if text.strip() else []


def _data_lines(data: dict[str, Any]) -> tuple[str, list[str]]:
    """A result's label and lines, from its best text form."""
    for mime in _TEXT_MIMES:
        if mime in data:
            value = data[mime]
            text = _text(value) if isinstance(value, str | list) else json.dumps(value, indent=1)
            return mime, _lines(text)
    kinds = ", ".join(sorted(str(key) for key in data)) or "empty"
    return kinds, [f"[{kinds} output, not shown]"]


def _output_lines(output: dict[str, Any]) -> list[str]:
    kind = output.get("output_type")
    if kind == "stream":
        name = output.get("name", "stdout")
        return [f"── output · {name} ──", *_lines(_ANSI.sub("", _text(output.get("text"))))]
    if kind in ("execute_result", "display_data"):
        data = output.get("data")
        label, body = _data_lines(data if isinstance(data, dict) else {})
        tag = "result" if kind == "execute_result" else "display"
        return [f"── output · {tag} · {label} ──", *body]
    if kind == "error":
        head = f"{output.get('ename', 'Error')}: {output.get('evalue', '')}".rstrip(": ")
        trace = output.get("traceback")
        frames = trace if isinstance(trace, list) else []
        body = [
            line
            for frame in frames
            if isinstance(frame, str)
            for line in _ANSI.sub("", frame).split("\n")
        ]
        return [f"── output · error · {head} ──", *body]
    return [f"── output · {kind} ──"]


def render_notebook(raw: str) -> tuple[str, list[str]] | None:
    """A header and the notebook's lines, or ``None`` if not nbformat 4."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict) or doc.get("nbformat") != 4:
        return None
    cells = doc.get("cells")
    if not isinstance(cells, list):
        return None
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    language = ""
    info = metadata.get("language_info") if isinstance(metadata, dict) else None
    if isinstance(info, dict) and isinstance(info.get("name"), str):
        language = info["name"]

    lines: list[str] = []
    for number, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        kind = str(cell.get("cell_type", "unknown"))
        label = f"── cell {number} · {kind}"
        count = cell.get("execution_count")
        if kind == "code" and isinstance(count, int):
            label += f" · In [{count}]"
        lines.append(label + " ──")
        lines.extend(_lines(_text(cell.get("source"))))
        outputs = cell.get("outputs")
        if kind == "code" and isinstance(outputs, list):
            for output in outputs:
                if isinstance(output, dict):
                    lines.extend(_output_lines(output))

    noun = "cell" if len(cells) == 1 else "cells"
    header = f"Notebook: {len(cells)} {noun}"
    if language:
        header += f", {language}"
    header += ". Line numbers count this view; the file itself is JSON."
    return header, lines
