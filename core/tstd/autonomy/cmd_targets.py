"""Redirect targets as ``cmd.exe`` reads a command line (TD-5157).

On Windows the interactive shell is ``cmd /c``. There ``\\`` is the path
separator and ``^`` is the escape, so the POSIX tokenizer the classifier
uses turns ``echo x > .git\\hooks\\pre-commit`` into a target named
``.githookspre-commit`` and the write never reads as one into ``.git``.

This reads the line the way cmd does: ``"`` opens and closes a quote
(inside one, ``>`` and ``^`` are plain text), ``^`` outside quotes makes
the next character plain, and ``>``, ``>>``, ``n>`` and ``>&`` redirect
output to the word after them, with or without a space. A target's
backslashes become ``/`` so it splits into folders on every host. Where
cmd's word end is unclear (``(``, ``)``, ``;``, ``,`` and ``=``), every
reading is returned: an extra target can only make a write Class C.
``%VAR%`` is left as written; the classifier matches names, not values.
"""

from __future__ import annotations

_WORD_END = frozenset(" \t\r\n&|<>")
_SOFT_ENDS = "();,="


def _word(text: str, i: int) -> tuple[str, int]:
    """The redirect target starting at *i* (after any blanks), unquoted and unescaped."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    out: list[str] = []
    quoted = False
    while i < n:
        c = text[i]
        if c == '"':
            quoted = not quoted
        elif quoted:
            out.append(c)
        elif c == "^":
            if i + 1 < n:
                out.append(text[i + 1])
            i += 1
        elif c in _WORD_END:
            break
        else:
            out.append(c)
        i += 1
    return "".join(out), i


def _readings(word: str) -> list[str]:
    """*word*, and each piece of it cut at a character cmd may end a word on."""
    readings = [word]
    piece: list[str] = []
    for c in word:
        if c in _SOFT_ENDS:
            readings.append("".join(piece))
            piece = []
        else:
            piece.append(c)
    readings.append("".join(piece))
    return [r.replace("\\", "/") for r in dict.fromkeys(readings) if r]


def cmd_write_targets(command: str) -> list[str]:
    """Files *command* redirects output into under ``cmd.exe``."""
    targets: list[str] = []
    n = len(command)
    i = 0
    quoted = False
    while i < n:
        c = command[i]
        if c == '"':
            quoted = not quoted
            i += 1
        elif quoted:
            i += 1
        elif c == "^":
            i += 2
        elif c == ">":
            i += 1
            if i < n and command[i] == ">":
                i += 1
            if i < n and command[i] == "&":
                i += 1  # >&2 duplicates a handle; a digit never names a protected file
            word, i = _word(command, i)
            targets += _readings(word)
        else:
            i += 1
    return targets
