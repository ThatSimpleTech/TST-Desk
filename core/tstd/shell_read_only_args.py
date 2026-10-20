"""How the read-only shell list reads a command line (TD-5103).

The list auto-runs a command with no approval, so its reader accepts far
less than ``sh`` does and refuses everything else:

- words made of ``A-Z a-z 0-9 _ - . / : = , + @ %`` (not ``=`` first;
  ``~`` and ``^`` inside a word, not after ``=`` or ``:``), single
  quotes, and double quotes with no ``$``, backtick or ``\\`` inside. So
  no expansion, substitution, glob, brace, tilde, comment, redirect,
  subshell, background ``&`` or escape is ever read, and the words are
  exactly what the program receives
- the separators ``;``, newline, ``|``, ``||`` and ``&&`` between simple
  commands. Every command must be on the list, so what a separator does
  never matters

Each command's options are then checked against the program's write,
exec and symlink-following flags (:data:`SPECS`, :func:`find_args`,
:func:`git_args`), and the words that can name a file come back for the
caller's path check.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

__all__ = ["SPECS", "Spec", "branch_args", "find_args", "git_args", "option_args", "segments"]

_BARE = frozenset(string.ascii_letters + string.digits + "_-./:=,+@%")
_MID_WORD = frozenset("~^")
# Never inside double quotes: the shell expands `$` and backticks there,
# and `\` escapes. A newline, CR or NUL is refused in any quote.
_DQUOTE_REFUSED = frozenset("$`\\\n\r\0")
_SQUOTE_REFUSED = frozenset("\n\r\0")


def segments(command: str) -> list[list[str]] | None:
    """The simple commands of *command* as the words each program receives.

    None when the line uses anything outside the subset in the module
    docstring. Empty commands (a trailing ``;``, a blank line) are dropped:
    the shell runs nothing for them or refuses the line.
    """
    segs: list[list[str]] = []
    words: list[str] = []
    word: list[str] | None = None
    i, n = 0, len(command)

    def close_word() -> None:
        nonlocal word
        if word is not None:
            words.append("".join(word))
            word = None

    def close_seg() -> None:
        nonlocal words
        close_word()
        if words:
            segs.append(words)
        words = []

    while i < n:
        c = command[i]
        if c in " \t":
            close_word()
            i += 1
        elif c in ";\n":
            close_seg()
            i += 1
        elif command.startswith(("&&", "||"), i):
            close_seg()
            i += 2
        elif c == "|":
            if command.startswith("|&", i):
                return None
            close_seg()
            i += 1
        elif c in "'\"":
            end = command.find(c, i + 1)
            if end < 0:
                return None
            body = command[i + 1 : end]
            refused = _SQUOTE_REFUSED if c == "'" else _DQUOTE_REFUSED
            if any(ch in refused for ch in body):
                return None
            word = (word or []) + [body]
            i = end + 1
        elif c in _BARE:
            if word is None and c == "=":
                return None  # zsh's `=cmd` names a program by path
            word = (word or []) + [c]
            i += 1
        elif c in _MID_WORD and word and word[-1][-1:] not in ("", "=", ":"):
            # `HEAD~1`, `HEAD^`: literal mid-word. A leading `~`, or one
            # after `=` or `:`, is a home directory to bash.
            word.append(c)
            i += 1
        else:
            return None
    close_seg()
    return segs or None


@dataclass(frozen=True)
class Spec:
    """A program's options that write, run another program, read a list
    of file names, or follow symlinks out of the wall during a walk.

    ``refused_short`` are single letters, refused wherever they sit in a
    cluster (``-laL``). ``refused_long`` are names without the dashes. Any
    long name that is a prefix of a refused one is refused too, because
    GNU ``getopt_long`` and git accept a unique abbreviation (``--deref``).
    """

    refused_short: str = ""
    refused_long: frozenset[str] = frozenset()


SPECS: dict[str, Spec] = {
    "ls": Spec("L", frozenset({"dereference"})),
    "pwd": Spec(),
    "cat": Spec(),
    "head": Spec(),
    "tail": Spec(),
    "wc": Spec("", frozenset({"files0-from"})),
    "file": Spec("CfzZ", frozenset({"compile", "files-from", "uncompress", "uncompress-noreport"})),
    "stat": Spec(),
    # -R follows every symlink in GNU grep; BSD grep (macOS) follows them
    # with -S, and with -O those named on the command line.
    "grep": Spec("ROS", frozenset({"dereference-recursive"})),
    "rg": Spec("Lz", frozenset({"follow", "pre", "pre-glob", "search-zip", "hostname-bin"})),
    "git": Spec("", frozenset({"output", "ext-diff", "show-signature"})),
}


def _long_refused(spec: Spec, name: str) -> bool:
    return any(r.startswith(name) for r in spec.refused_long)


def option_args(spec: Spec, args: list[str]) -> list[str] | None:
    """The words of *args* to path-check, or None when a refused option is used.

    Operands are checked, and so is any value attached to an option
    (``--file=x``, the ``x`` of ``-fx``). A value given as the next word is
    an operand here, so it is checked too: a pattern that starts with ``/``
    asks, which costs a prompt and nothing else. Options are read after
    operands as well, as GNU tools read them, until ``--``.
    """
    checks: list[str] = []
    options = True
    for word in args:
        if not options or word == "-" or not word.startswith("-"):
            if word != "-":
                checks.append(word)
            continue
        if word == "--":
            options = False
            continue
        if word.startswith("--"):
            name, eq, value = word[2:].partition("=")
            if _long_refused(spec, name):
                return None
            if eq:
                checks.append(value)
            continue
        if any(c in spec.refused_short for c in word[1:]):
            return None
        if len(word) > 2:
            checks.append(word[2:])
    return checks


# find's actions that run a program, delete, or write a file, its options
# that follow symlinks during the walk, and GNU 4.9's list of start points.
_FIND_REFUSED = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
    | {"-fprint", "-fprint0", "-fprintf", "-fls"}
    | {"-L", "-follow", "-files0-from"}
)


def find_args(args: list[str]) -> list[str] | None:
    """``find``'s words to path-check: start points and primaries' operands.

    Words starting with ``-`` are primaries or numbers (``-size -5k``) and
    are matched whole, since find does not abbreviate. Everything else is
    checked as a path, patterns included (``-name 'x'`` stays inside).
    """
    checks: list[str] = []
    for word in args:
        if word.startswith("-"):
            if word in _FIND_REFUSED:
                return None
            continue
        checks.append(word)
    return checks


# `git branch` creates, deletes, renames or re-points with almost any
# option, and with a bare name: only these list, and a name is allowed
# only when one of _BRANCH_LISTS forces list mode (`git branch -v x`
# creates x).
_BRANCH_SHORT = frozenset("lavqri")
_BRANCH_LONG = frozenset(
    {"list", "all", "remotes", "contains", "no-contains", "merged", "no-merged", "points-at"}
    | {"verbose", "quiet", "abbrev", "no-abbrev", "color", "no-color", "column", "no-column"}
    | {"sort", "format", "ignore-case", "omit-empty", "show-current"}
)
_BRANCH_LISTS_SHORT = frozenset("lar")
_BRANCH_LISTS_LONG = frozenset(
    {"list", "all", "remotes", "contains", "no-contains", "merged", "no-merged", "points-at"}
)


def branch_args(args: list[str]) -> list[str] | None:
    """``git branch``'s words to path-check, or None unless it only lists."""
    checks: list[str] = []
    operands = False
    lists = False
    options = True
    for word in args:
        if not options or not word.startswith("-") or word == "-":
            checks.append(word)
            operands = True
            continue
        if word == "--":
            options = False
            continue
        if word.startswith("--"):
            name, eq, value = word[2:].partition("=")
            if name not in _BRANCH_LONG:
                return None
            lists = lists or name in _BRANCH_LISTS_LONG
            if eq:
                checks.append(value)
            continue
        letters = set(word[1:])
        if not letters <= _BRANCH_SHORT:
            return None
        lists = lists or bool(letters & _BRANCH_LISTS_SHORT)
    if operands and not lists:
        return None
    return checks


# Options before the subcommand: only the ones that change nothing it reads.
_GIT_GLOBAL = frozenset({"--no-pager", "-P", "--no-optional-locks"})
GIT_SUBCOMMANDS = ("status", "log", "diff", "show", "branch")


def git_args(args: list[str]) -> tuple[str, list[str]] | None:
    """The subcommand and the words to path-check, or None.

    No global option but :data:`_GIT_GLOBAL` is taken: ``-c`` sets config
    (``core.fsmonitor`` runs a command), and ``-C``, ``--git-dir``,
    ``--work-tree`` and ``--exec-path`` move what git reads or runs.
    """
    k = 0
    while k < len(args) and args[k] in _GIT_GLOBAL:
        k += 1
    if k >= len(args) or args[k] not in GIT_SUBCOMMANDS:
        return None
    sub, rest = args[k], args[k + 1 :]
    checks = branch_args(rest) if sub == "branch" else option_args(SPECS["git"], rest)
    return None if checks is None else (sub, checks)
