"""How policy reads a shell command line (TD-5101, TD-5156).

Policy rules are globs over text. One glob over a whole command line let an
``auto`` rule for ``npm test*`` also run ``npm test; curl x | sh``. This
module splits the line into the commands the shell will run, so policy can
judge each one:

- at ``;``, ``&``, ``&&``, ``|``, ``||``, newlines and subshell parentheses,
  after removing each ``\\``-newline as the shell does
- into the commands inside ``$(…)``, backticks, ``<(…)`` and ``>(…)``, the
  ``$(…)`` in an unquoted heredoc, the string handed to ``eval``, ``sh -c``
  or ``cmd /c``, an ``alias`` value and a ``trap`` action

Each command gets two readings. The *allow* text is what an ``auto`` rule
must match: the command as written, with only the wrappers that never change
what runs (``timeout``, ``nice``, ``nohup``, ``time``, ``stdbuf``) taken off
the front. An environment assignment stays on, since ``LD_PRELOAD=…`` or
``NODE_OPTIONS=…`` change what a command does. The *deny* texts are what
``ask`` and ``never`` rules are checked against: the command as written,
unquoted, and with each wrapper, keyword, redirect, ``env``, ``sudo`` or
``xargs`` peeled off the front in turn, each also with the program named
without its folder (``/usr/bin/curl`` reads as ``curl``).

Reading less than the shell runs is the failure that matters, so this module
errs toward reading more. Where the shells disagree, or the text is too odd
to split with confidence (``$'…'``, ``$((…))``, ``((…))``, ``case`` inside a
substitution, an unclosed quote, a heredoc whose end bash and dash place
apart, a ``\\``-newline that joins two halves of an operator, cmd's
``for /f``), the result is incomplete: no ``auto`` rule applies, and ``ask``
and ``never`` rules still see everything read so far. A differential test
(``tests/test_policy_shell_differential.py``) runs a corpus under bash, bash
``--posix``, dash and busybox ash and holds the scanner to that. Nothing
here runs a shell.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .policy_shell_scan import Dialect, Scanner, Seg
from .policy_shell_words import allow_start, deny_readings, unquote

__all__ = ["Command", "Dialect", "Split", "host_dialects", "split_command"]


@dataclass(frozen=True)
class Command:
    """One command of a line: the text ``auto`` rules must match, and the
    texts ``ask`` and ``never`` rules are checked against."""

    allow: str
    deny: tuple[str, ...]


@dataclass(frozen=True)
class Split:
    """A command line as policy reads it.

    ``complete`` is False when the line could not be split with confidence;
    then no ``auto`` rule may apply to it.
    """

    commands: tuple[Command, ...]
    complete: bool

    @property
    def allow_texts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.allow for c in self.commands))

    @property
    def deny_texts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t for c in self.commands for t in c.deny))


def host_dialects() -> tuple[Dialect, ...]:
    """The shells a command may run under on this host.

    Windows runs interactive commands through ``cmd /c`` and autonomy runs
    through ``sh -c`` in a Linux container, so it is read both ways.
    """
    return ("posix", "cmd") if sys.platform == "win32" else ("posix",)


def split_command(command: str, dialects: tuple[Dialect, ...] | None = None) -> Split:
    """Split *command* into the commands each dialect would run."""
    commands: list[Command] = []
    complete = True
    for dialect in dialects or host_dialects():
        scanner = Scanner(command, dialect, 0)
        scanner.run()
        commands.extend(_command(seg, dialect) for seg in scanner.segs)
        complete = complete and scanner.complete
    return Split(tuple(commands), complete)


def _command(seg: Seg, dialect: Dialect) -> Command:
    raw = seg.raw_words()
    k = allow_start(raw)
    start = seg.words[k][0] if k < len(raw) else seg.start
    allow = "\n".join([seg.source[start : seg.end].strip(), *seg.bodies])
    words = [unquote(w, dialect == "posix") for w in raw]
    deny = [seg.text, allow, *deny_readings(words)]
    return Command(allow, tuple(dict.fromkeys(t for t in deny if t)))
