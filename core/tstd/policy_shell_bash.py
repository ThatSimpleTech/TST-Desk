"""bash-only words the shell scanner fails closed on (TD-5156).

bash reads some words whole where dash and ash see structure: bash 5.3's
``${ cmd; }``, ``[[ … ]]``, arrays and extglob patterns glued to a ``(``, and
``name[…]`` subscripts. Inside them a ``#``, ``(`` or ``<<`` is text to bash
but a comment, a subshell or a heredoc to the scanner in
:mod:`tstd.policy_shell_scan`, so each check here raises ``Incomplete``
where the two readings could part.
"""

from __future__ import annotations

import re

from .policy_shell_words import Incomplete

# What may follow `${`: a name, a digit or a special parameter. bash 5.3
# runs `${ cmd; }` and `${| cmd; }` as commands; dash rejects them.
_PARAMETER_START = re.compile(r"[A-Za-z0-9_@*#?$!-]")
# A word that is a NAME so far, so a `[` opens a subscript bash reads whole.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A subscript with nothing in it the scanner would read as structure.
_PLAIN_SUBSCRIPT = re.compile(r"\[(?:[^\s;&|<>()#'\"`\\$\[\]{}]|\$(?=[A-Za-z0-9_]))*\]")
# `?(`, `*(`, `+(`, `@(` and `!(` open an extglob pattern in bash.
_EXTGLOB = frozenset("?*+@!")


def _word(text: str, start: int, end: int) -> str:
    return text[start:end].replace("\\\n", "")


def parameter(text: str, i: int) -> None:
    """*i* is just after ``${``."""
    if not _PARAMETER_START.match(text, i):
        raise Incomplete


def closed_word(word: str) -> None:
    """A word as the scanner closes it, ``\\``-newlines removed."""
    if word == "[[":
        # bash reads `( #)` after `=~`, and `@( #)` after `==`, as part of
        # one word; dash runs `[[` as a command.
        raise Incomplete


def glued_paren(text: str, word_start: int, i: int) -> None:
    """A ``(`` at *i* that continues a word: only ``name()`` reads the same
    in every shell. bash reads ``a=(…)`` (an array), ``@(…)`` and the other
    extglob forms as one word; elsewhere they are syntax errors."""
    word = _word(text, word_start, i)
    if not (text.startswith("()", i) and word[-1:] not in _EXTGLOB):
        raise Incomplete


def subscript(text: str, word_start: int, i: int) -> None:
    """A ``[`` at *i* after ``name`` opens a subscript bash reads whole
    (``a[1<<2]=x``): anything in it the scanner reads as structure is
    incomplete."""
    if _NAME.fullmatch(_word(text, word_start, i)) and not _PLAIN_SUBSCRIPT.match(text, i):
        raise Incomplete
