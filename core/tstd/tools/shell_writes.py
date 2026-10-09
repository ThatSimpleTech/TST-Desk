"""Spot shell commands that write workspace files (TD-5009).

``fs_write`` and ``fs_edit`` declare their paths, so the dispatcher
snapshots them for a diff (TD-604) and checkpoints them (TD-705), and the
progress guard counts them as progress (TD-5002).  A shell command that
writes a file gets none of that: the Files and Work panes never show the
change and there is no checkpoint to revert to.

This module reads the command text and nothing else.  It runs no process,
touches no file, and never changes how or whether a command runs:
``run_shell`` adds :data:`SHELL_WRITE_NOTE` to the result when
:func:`writes_workspace_files` says yes.  It is a nudge, so the rules cover
the common write forms and stay quiet on read-only commands.  An odd
command that slips past costs nothing.  It is not a security control: the
classifier's own target extraction (TD-4805) keeps shell writes off
steering files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .shell_write_forms import (
    INTERPRETER,
    INTERPRETER_IN_LINE,
    code_writes,
    gawk_in_place,
    inline_code,
    perl_files,
    sed_files,
)

SHELL_WRITE_NOTE = (
    "[note: this command wrote files through the shell. Use fs_edit or fs_write "
    "for file changes so they show in the diff and can be reviewed.]"
)

_REDIRECTS = frozenset({">", ">>", ">|", ">&", "&>", "&>>"})
_SEPARATORS = frozenset({"|", "||", "|&", "&", "&&", ";", ";;", "\n"})
# Longest first, so `&>>` is not read as `&>` then `>`.
_OPERATORS = tuple(sorted(_REDIRECTS | _SEPARATORS, key=len, reverse=True))
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "("})
_WRAPPERS = frozenset(
    {"sudo", "doas", "env", "command", "builtin", "exec", "nohup", "nice", "time", "timeout"}
    | {"stdbuf", "xargs"}
)
_PROGRAM_WORD = re.compile(r"^[A-Za-z0-9_./+-]+$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TEMP_VARS = ("$TMPDIR", "${TMPDIR}", "$TMP", "${TMP}", "$TEMP", "${TEMP}")
_HOME_PREFIXES = ("~", "$HOME", "${HOME}")
# `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`, `<<\EOF`; not the `<<<` here-string.
_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)(-?)\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))"
)


@dataclass(frozen=True)
class _Token:
    text: str
    operator: bool = False
    quoted: bool = False


@dataclass(frozen=True)
class _Scope:
    """The workspace a target must fall inside to count."""

    roots: tuple[str, ...]
    home: str

    @classmethod
    def of(cls, workspace: str | Path) -> _Scope:
        raw = os.path.normpath(os.path.abspath(workspace))
        roots = tuple(dict.fromkeys((raw, os.path.realpath(raw))))
        return cls(roots=roots, home=os.path.expanduser("~"))

    def expand_home(self, target: str) -> str:
        for prefix in _HOME_PREFIXES:
            if target == prefix or target.startswith(prefix + "/"):
                return self.home + target[len(prefix) :]
        return target

    def inside(self, raw: str, cwd: str | None) -> bool:
        """Whether a write to *raw*, run from *cwd*, lands in the workspace.

        Scratch space (``/dev``, ``$TMPDIR``), git's own ``.git/`` files
        and absolute paths elsewhere do not count.  An expansion this
        cannot follow (``"$out"``) and a relative path after an unknown
        ``cd`` do count: a nudge on a real write matters more than silence
        on a rare scratch one.
        """
        target = raw.strip().lstrip("!")  # zsh clobber mark: `>!file`
        if target.count(")") > target.count("("):
            target = target.rstrip(")")  # `(cd x && echo > y)`
        if not target or target.startswith("("):
            return False  # process substitution: `tee >(gzip > f)`
        if target == "/dev" or target.startswith("/dev/") or "mktemp" in target:
            return False
        if any(target == var or target.startswith(var + "/") for var in _TEMP_VARS):
            return False
        target = self.expand_home(target)
        if "$" in target or "`" in target:
            return True
        if os.path.isabs(target):
            path = os.path.normpath(target)
        elif cwd is None:
            return True
        else:
            path = os.path.normpath(os.path.join(cwd, target))
        for root in self.roots:
            prefix = root.rstrip(os.sep) + os.sep
            if path.startswith(prefix):
                # Git's own files (`.git/info/exclude`) are not reviewable work.
                rest = path[len(prefix) :]
                return rest != ".git" and not rest.startswith(".git" + os.sep)
        return False

    def next_cwd(self, arg: str | None, cwd: str | None) -> str | None:
        """The directory after ``cd arg``; None when it cannot be known."""
        if arg is None:
            return self.home
        target = self.expand_home(arg)
        if target == "-" or "$" in target or "`" in target:
            return None
        if os.path.isabs(target):
            return os.path.normpath(target)
        return None if cwd is None else os.path.normpath(os.path.join(cwd, target))


def writes_workspace_files(command: str, workspace: str | Path) -> bool:
    """Whether *command* looks like it writes a file inside *workspace*.

    Covers output redirection (not descriptor dups, ``/dev``, temp space,
    or absolute paths outside the workspace), ``sed -i``, ``perl -i``,
    ``gawk -i inplace``, ``tee``, ``dd of=``, inline ``python -c`` /
    ``node -e`` code (or a heredoc fed to them) that opens a file for
    writing, and a heredoc with a redirect.  A command that cannot be
    tokenized is not flagged.
    """
    scope = _Scope.of(workspace)
    text, heredocs = _split_heredocs(command)
    for opener, body in heredocs:
        if INTERPRETER_IN_LINE.search(opener) and code_writes(
            body, partial(scope.inside, cwd=scope.roots[0])
        ):
            return True
    tokens = _tokenize(text)
    if tokens is None:
        return False
    cwd: str | None = scope.roots[0]
    for segment in _segments(tokens):
        wrote, cwd = _segment_writes(segment, scope, cwd)
        if wrote:
            return True
    return False


def _split_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Cut heredoc bodies out of *command*: (text without them, [(opener line, body)]).

    A body is data, not shell: `>` in a heredoc of HTML or Python is not a
    redirect.  An unterminated body runs to the end, as in the shell.
    """
    lines = command.split("\n")
    kept: list[str] = []
    bodies: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in _HEREDOC.finditer(line):
            strip_tabs = match.group(1) == "-"
            word = match.group(2) or match.group(3) or match.group(4)
            body: list[str] = []
            while i < len(lines):
                candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if candidate.strip() == word:
                    break
                body.append(lines[i - 1])
            bodies.append((line, "\n".join(body)))
    return "\n".join(kept), bodies


def _tokenize(command: str) -> list[_Token] | None:
    """POSIX-ish words and unquoted operators; None on an unclosed quote."""
    tokens: list[_Token] = []
    buf: list[str] = []
    in_word = False
    quoted = False
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal in_word, quoted
        if in_word:
            tokens.append(_Token("".join(buf), quoted=quoted))
        buf.clear()
        in_word = quoted = False

    while i < n:
        ch = command[i]
        if ch in " \t\r":
            flush()
            i += 1
        elif ch == "\\":
            if command.startswith("\\\n", i):
                i += 2  # line continuation
                continue
            buf.append(command[i + 1 : i + 2])
            in_word = quoted = True
            i += 2
        elif ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            buf.append(command[i + 1 : end])
            in_word = quoted = True
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '"\\$`\n':
                    j += 1
                buf.append(command[j])
                j += 1
            if j >= n:
                return None
            in_word = quoted = True
            i = j + 1
        elif ch == "#" and not in_word:
            end = command.find("\n", i)
            i = n if end < 0 else end
        else:
            op = next((o for o in _OPERATORS if command.startswith(o, i)), None)
            if op is None:
                buf.append(ch)
                in_word = True
                i += 1
            else:
                flush()
                tokens.append(_Token(op, operator=True))
                i += len(op)
    flush()
    return tokens


def _segments(tokens: list[_Token]) -> list[list[_Token]]:
    segments: list[list[_Token]] = [[]]
    for token in tokens:
        if token.operator and token.text in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _segment_writes(
    segment: list[_Token], scope: _Scope, cwd: str | None
) -> tuple[bool, str | None]:
    """(does this simple command write the workspace, the cwd after it)."""
    words, wrote = _redirect_pass(segment, scope, cwd)
    start = _program_index(words)
    if start is None:
        return wrote, cwd
    name = _program_name(words[start])
    args = words[start + 1 :]
    if name in ("cd", "pushd"):
        return wrote, scope.next_cwd(args[0] if args else None, cwd)
    return wrote or _program_writes(name, args, scope, cwd), cwd


def _redirect_pass(segment: list[_Token], scope: _Scope, cwd: str | None) -> tuple[list[str], bool]:
    """Judge the segment's redirections; return its other words and the verdict."""
    words: list[str] = []
    wrote = False
    depth = 0  # inside `[[ ]]` or `(( ))`, `>` compares rather than redirects
    i = 0
    while i < len(segment):
        token = segment[i]
        i += 1
        if not token.operator:
            words.append(token.text)
            if not token.quoted:
                opened = token.text.count("((") + (token.text == "[[")
                closed = token.text.count("))") + (token.text == "]]")
                depth = max(0, depth + opened - closed)
            continue
        if depth or i >= len(segment) or segment[i].operator:
            continue
        target = segment[i].text
        i += 1
        if words and words[-1].isdigit():
            words.pop()  # the descriptor of `2>`
        if token.text == ">&" and (target.isdigit() or target == "-"):
            continue  # `2>&1`, `>&2`, `>&-`
        wrote = wrote or scope.inside(target, cwd)
    return words, wrote


def _program_index(words: list[str]) -> int | None:
    """Index of the command word, past assignments, keywords and wrappers."""
    wrapped = False
    for i, raw in enumerate(words):
        word = raw.lstrip("({")
        if not word or word in _KEYWORDS or _ASSIGNMENT.match(word):
            continue
        if wrapped and (word.startswith("-") or word[0].isdigit() or not _PROGRAM_WORD.match(word)):
            continue  # a wrapper's own options: `sudo -n`, `timeout 10`, `xargs -I {}`
        if _program_name(word) in _WRAPPERS:
            wrapped = True
            continue
        return i
    return None


def _program_name(word: str) -> str:
    return os.path.basename(word.lstrip("({"))


def _program_writes(name: str, args: list[str], scope: _Scope, cwd: str | None) -> bool:
    if name == "tee":
        return any(scope.inside(arg, cwd) for arg in args if arg and not arg.startswith("-"))
    if name == "dd":
        return any(arg.startswith("of=") and scope.inside(arg[3:], cwd) for arg in args)
    if name in ("sed", "gsed"):
        return _in_place_hits(sed_files(args), scope, cwd)
    if name == "perl":
        return _in_place_hits(perl_files(args), scope, cwd)
    if name in ("awk", "gawk"):
        return gawk_in_place(args)
    if name == "find":
        return _find_exec_writes(args, scope, cwd)
    if INTERPRETER.match(name):
        code = inline_code(name, args)
        return code is not None and code_writes(code, partial(scope.inside, cwd=cwd))
    return False


def _in_place_hits(files: list[str] | None, scope: _Scope, cwd: str | None) -> bool:
    """An in-place edit counts unless every named file is outside the workspace.

    No file operand means `xargs` or `find -exec` feeds the names, which
    are workspace files far more often than not.
    """
    if files is None:
        return False
    named = [f for f in files if f]
    return not named or any(scope.inside(f, cwd) for f in named)


def _find_exec_writes(args: list[str], scope: _Scope, cwd: str | None) -> bool:
    """``find ... -exec sed -i ... {} +`` runs a writer per match."""
    for i, arg in enumerate(args):
        if arg not in ("-exec", "-execdir", "-ok", "-okdir"):
            continue
        sub: list[str] = []
        for word in args[i + 1 :]:
            if word in (";", "+"):
                break
            sub.append(word)
        start = _program_index(sub)
        if start is not None and _program_writes(
            _program_name(sub[start]), sub[start + 1 :], scope, cwd
        ):
            return True
    return False
