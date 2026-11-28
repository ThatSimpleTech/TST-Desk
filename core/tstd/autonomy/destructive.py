"""What a shell command would delete or overwrite, read from its words (APPROVE-02).

Every shell command already asks (the B floor, TD-4805). This tells the
card which ones are dangerous: a command that deletes, moves over,
truncates or rewrites the permissions of something outside the workspace,
or one that runs with raised privileges. Those rate Class C, so the card
shows them as dangerous and never offers Always allow.

It reads the common forms only, the way ``_shell_write_targets`` reads
redirections: ``rm``, ``rmdir``, ``unlink``, ``shred``, ``mv``, ``cp``
(its destination), ``dd of=``, ``truncate``, ``chmod``/``chown``/``chgrp``,
``find … -delete`` and ``find … -exec rm``, and disk tools that need no
target to be dangerous. A target it cannot place (``$VAR``, ``~``, a
command substitution) counts as outside: that can only make a command
dangerous, never safe. A line it cannot tokenize yields nothing here and
keeps the B floor.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import PurePath

_SEPARATORS = frozenset({"|", "&", ";", "||", "&&", "|&", ";;", "(", ")"})
_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">&", ">|", "<", "<<", "<<<"})

#: Run what follows with raised privileges.
PRIVILEGED = frozenset({"sudo", "doas", "pkexec", "su", "run0"})
#: Run what follows as it is (their own options are skipped).
_WRAPPERS = frozenset({"env", "command", "nice", "nohup", "time", "timeout", "exec", "builtin"})
#: Dangerous with any arguments: they act on disks and filesystems.
_ALWAYS = frozenset({"mkfs", "wipefs", "fdisk", "sfdisk", "parted", "mkswap", "blkdiscard"})
#: Every operand is deleted, moved or emptied.
_EVERY_OPERAND = frozenset({"rm", "rmdir", "unlink", "shred", "srm", "mv", "truncate"})
#: The first operand is a mode or owner; the rest are changed.
_AFTER_FIRST = frozenset({"chmod", "chown", "chgrp"})
#: The last operand is overwritten.
_LAST_OPERAND = frozenset({"cp", "install", "rsync"})


@dataclass
class Danger:
    """What one command line would delete or overwrite, and how it runs."""

    targets: list[str] = field(default_factory=list)
    #: Where ``find … -delete`` starts: what is under it goes, not it.
    search_roots: list[str] = field(default_factory=list)
    privileged: bool = False
    disk_tool: bool = False


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _without_redirects(words: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word in _REDIRECTS:
            skip = True
            continue
        out.append(word)
    return out


def _strip_prefix(words: list[str], danger: Danger) -> list[str]:
    """Drop ``FOO=bar`` assignments and wrappers; note a privilege raise."""
    i = 0
    while i < len(words):
        word = words[i]
        name = PurePath(word).name
        if "=" in word and not word.startswith("-") and name == word and word.index("=") > 0:
            i += 1
        elif name in PRIVILEGED:
            danger.privileged = True
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif name in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or "=" in words[i]):
                i += 1
            if name == "timeout" and i < len(words):
                i += 1  # its duration
        else:
            break
    return words[i:]


def _operands(args: list[str]) -> list[str]:
    out: list[str] = []
    ended = False
    for arg in args:
        if not ended and arg == "--":
            ended = True
        elif ended or not arg.startswith("-") or arg == "-":
            out.append(arg)
    return out


def _find_targets(args: list[str]) -> list[str]:
    deletes = "-delete" in args or any(
        a in ("-exec", "-execdir", "-ok", "-okdir")
        and i + 1 < len(args)
        and PurePath(args[i + 1]).name in _EVERY_OPERAND
        for i, a in enumerate(args)
    )
    if not deletes:
        return []
    starts: list[str] = []
    for arg in args:
        if arg.startswith(("-", "!", "(")):
            break
        starts.append(arg)
    return starts or ["."]


def _segment_targets(words: list[str], danger: Danger) -> None:
    words = _strip_prefix(_without_redirects(words), danger)
    if not words:
        return
    name = PurePath(words[0]).name
    args = words[1:]
    if name in _ALWAYS or name.startswith("mkfs."):
        danger.disk_tool = True
    elif name in _EVERY_OPERAND:
        danger.targets += _operands(args)
    elif name in _AFTER_FIRST:
        danger.targets += _operands(args)[1:]
    elif name in _LAST_OPERAND:
        operands = _operands(args)
        if len(operands) >= 2:
            danger.targets.append(operands[-1])
    elif name == "dd":
        danger.targets += [a[3:] for a in args if a.startswith("of=")]
    elif name == "find":
        danger.search_roots += _find_targets(args)


def read_danger(command: str) -> Danger:
    """The targets ``command`` would delete or overwrite, best effort."""
    danger = Danger()
    try:
        segments = _segments(command)
    except ValueError:
        return danger
    for words in segments:
        _segment_targets(words, danger)
    return danger


def unplaceable(target: str) -> bool:
    """A target whose place only the shell knows (``$HOME``, ``~``, ``$(…)``, a backtick)."""
    return target.startswith("~") or "$" in target or "`" in target
