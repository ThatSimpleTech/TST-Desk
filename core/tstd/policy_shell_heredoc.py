"""Here-documents and line continuations, as policy reads them (TD-5101, TD-5156).

Where a heredoc's body ends decides which later lines are data and which
are commands, and the shells do not all agree. bash joins a body line that
ends in ``\\`` to the next before it looks for the delimiter; dash and ash
do not. Each function here reads the text one way, and the scanner in
:mod:`tstd.policy_shell_scan` marks the line incomplete wherever the
readings part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .policy_shell_words import Incomplete

_DELIMITER_STOP = frozenset(" \t\n;&|<>()")
_ESCAPE = re.compile(r"\\(.)", re.S)


@dataclass
class Heredoc:
    delimiter: str
    strip_tabs: bool
    quoted: bool
    #: The segment's list of bodies, which this one's body joins.
    bodies: list[str]


def skip_splices(text: str, i: int) -> int:
    """The index after any ``\\``-newlines at *i*, which the shell removes."""
    while text.startswith("\\\n", i):
        i += 2
    return i


def read_operator(text: str, i: int, bodies: list[str]) -> tuple[Heredoc, int]:
    """Read ``<<word`` or ``<<-word`` at *i*; return it and the index after.

    A ``\\``-newline anywhere in it is removed first, as the shell does.
    """
    i = skip_splices(text, i + 2)
    if text.startswith("<", i):
        raise Incomplete  # bash joins `<<` `\`-newline `<` into a here-string
    strip_tabs = text.startswith("-", i)
    i += strip_tabs
    while True:
        if text.startswith((" ", "\t"), i):
            i += 1
        elif (after := skip_splices(text, i)) > i:
            i = after
        else:
            break
    parts: list[str] = []
    quoted = False
    while i < len(text) and text[i] not in _DELIMITER_STOP:
        c = text[i]
        if c in "$`":
            raise Incomplete
        if text.startswith("\\\n", i):
            i += 2
        elif c == "\\":
            quoted = True
            parts.append(text[i + 1 : i + 2])
            i += 2
        elif c in "'\"":
            end = text.find(c, i + 1)
            if end < 0 or (c == '"' and "\\" in text[i:end]):
                raise Incomplete  # inside "…" a backslash escapes or joins lines
            quoted = True
            parts.append(text[i + 1 : end])
            i = end + 1
        else:
            parts.append(c)
            i += 1
    delimiter = "".join(parts)
    if not delimiter:
        raise Incomplete
    return Heredoc(delimiter, strip_tabs, quoted, bodies), i


def body_end(text: str, i: int, doc: Heredoc, *, in_sub: bool) -> tuple[int, int]:
    """Where the body starting at *i* ends, and where reading resumes.

    Raises :class:`Incomplete` where shells end it in different places.
    """
    found = _end(text, i, doc, joined=False, in_sub=in_sub)
    # bash joins a line ending in `\` to the next before it looks for the
    # delimiter in an unquoted body; dash and ash do not.
    if not doc.quoted and _end(text, i, doc, joined=True, in_sub=in_sub) != found:
        raise Incomplete
    return found


def _end(text: str, pos: int, doc: Heredoc, *, joined: bool, in_sub: bool) -> tuple[int, int]:
    n = len(text)
    while pos < n:
        line_start, line = pos, ""
        while True:
            newline = text.find("\n", pos)
            stop = n if newline < 0 else newline
            part = text[pos:stop]
            pos = stop + 1
            backslashes = len(part) - len(part.rstrip("\\"))
            if not (joined and newline >= 0 and backslashes % 2):
                line += part
                break
            line += part[:-1]
        line = line.lstrip("\t") if doc.strip_tabs else line
        if line == doc.delimiter:
            return line_start, min(pos, n)
        if in_sub and line.startswith(doc.delimiter):
            raise Incomplete  # inside $( ), bash also ends the body at `EOF)`
    return n, n


def unsplice(body: str) -> str:
    """*body* with each ``\\``-newline removed and other escapes kept, as
    bash reads an unquoted body before it runs the body's substitutions."""
    return _ESCAPE.sub(lambda m: "" if m.group(1) == "\n" else m.group(0), body)
