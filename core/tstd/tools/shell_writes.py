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
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .shell_syntax import INPUT_REDIRECTS, PIPES, Segment, Token, segments, tokenize
from .shell_write_forms import (
    INTERPRETER,
    code_writes,
    gawk_in_place,
    interpreter_source,
    perl_files,
    sed_files,
    shell_source,
)

# Forward-looking on purpose: the detector reads text, so the write may
# have failed, and a model told the write "should" have gone through
# fs_write must not send the whole file again to comply.
SHELL_WRITE_NOTE = (
    "[note: this command looks like it changed workspace files through the "
    "shell. For your next file change, use fs_edit or fs_write so it shows in "
    "the diff; no need to redo this one.]"
)

_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "("})
#: Programs that run the next word as a command, with their options that
#: take a separate value (`env -u NAME`, `sudo -u USER`, `timeout -s SIG`),
#: so that value is not read as the program.
_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "xargs": frozenset({"-I", "-a", "-d", "-E", "-L", "-n", "-P", "-s"})
    | frozenset({"--arg-file", "--delimiter", "--max-args", "--max-procs"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "exec": frozenset({"-a"}),
    **{name: frozenset() for name in ("command", "builtin", "nohup")},
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
#: `bash -c "bash -c '...'"` is read this many levels deep, no further.
_MAX_NESTING = 3
_PROGRAM_WORD = re.compile(r"^[A-Za-z0-9_./+-]+$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
#: `$NAME/...`, `${NAME}/...` or `${NAME:-default}/...` at the start of a path.
_VARIABLE_PREFIX = re.compile(r"^\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?::?[-=][^}]*)?\})(?=/|$)")
_HOME_PREFIXES = ("~", "$HOME", "${HOME}")
#: Null devices: cmd's `nul` and PowerShell's `$null` (a POSIX shell
#: expands `$null` to nothing and writes no file).
_NULL_DEVICES = frozenset({"nul", "nul:", "$null"})


@dataclass
class _Scope:
    """The workspace a target must fall inside to count."""

    roots: tuple[str, ...]
    home: str
    #: Variables that hold scratch space: `$TMPDIR`, or `out=$(mktemp)`.
    temp_names: set[str] = field(default_factory=lambda: {"TMPDIR", "TMP", "TEMP"})

    @classmethod
    def of(cls, workspace: str | Path, mount: str | None) -> _Scope:
        raw = os.path.normpath(os.path.abspath(workspace))
        mounted = (os.path.normpath(mount),) if mount else ()
        roots = tuple(dict.fromkeys((raw, os.path.realpath(raw), *mounted)))
        return cls(roots=roots, home=os.path.expanduser("~"))

    def expand_home(self, target: str) -> str:
        for prefix in _HOME_PREFIXES:
            if target == prefix or target.startswith(prefix + "/"):
                return self.home + target[len(prefix) :]
        return target

    def inside(self, raw: str, cwd: str | None) -> bool:
        """Whether a write to *raw*, run from *cwd*, lands in the workspace.

        Scratch space (``/dev``, null devices, ``$TMPDIR``, a ``mktemp``
        name), git's own ``.git/`` files and absolute paths elsewhere do
        not count.  An expansion this cannot follow (``"$out"``) and a
        relative path after an unknown ``cd`` do count: a nudge on a real
        write matters more than silence on a rare scratch one.
        """
        target = raw.strip().lstrip("!")  # zsh clobber mark: `>!file`
        if target.count(")") > target.count("("):
            target = target.rstrip(")")  # `(cd x && echo > y)`
        if not target or target.startswith("("):
            return False  # process substitution: `tee >(gzip > f)`
        if target.lower() in _NULL_DEVICES or target == "/dev" or target.startswith("/dev/"):
            return False
        variable = _VARIABLE_PREFIX.match(target)
        if "mktemp" in target or (
            variable is not None and (variable.group(1) or variable.group(2)) in self.temp_names
        ):
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


def writes_workspace_files(
    command: str, workspace: str | Path, *, mount: str | None = None
) -> bool:
    """Whether *command* looks like it writes a file inside *workspace*.

    Covers output redirection (not descriptor dups, ``/dev``, null
    devices, temp space, ``.log`` captures, or absolute paths outside the
    workspace), ``sed -i``, ``perl -i``, ``gawk -i inplace``, ``tee``,
    ``dd of=``, inline ``python -c`` / ``node -e`` code or a heredoc fed to
    them that opens a file for writing, and any of those inside ``sh -c``.
    A command that cannot be tokenized is not flagged.

    *mount* is where a container sees the workspace (autonomy mounts it at
    ``/workspace``); a path under it counts too.
    """
    scope = _Scope.of(workspace, mount)
    return _Walk(scope, scope.roots[0], 0).command_writes(command)


@dataclass
class _Walk:
    """One pass over a command's simple commands, tracking the cwd."""

    scope: _Scope
    cwd: str | None
    nesting: int
    #: The cwd outside each `( ... )` subshell entered so far.
    outer: list[str | None] = field(default_factory=list)
    #: Heredoc bodies `cat <<EOF |` sends down the pipe.
    piped: list[str] = field(default_factory=list)

    def command_writes(self, command: str) -> bool:
        tokens = tokenize(command)
        if tokens is None:
            return False
        return any(self._step(segment) for segment in segments(tokens))

    def _step(self, segment: Segment) -> bool:
        self.outer.extend([self.cwd] * _opened_subshells(segment.tokens))
        words, wrote, bodies = _redirect_pass(segment.tokens, self.scope, self.cwd)
        stdin = bodies or self.piped
        self.piped = []
        for word in words:
            assigned = _ASSIGNMENT.match(word)
            if assigned is not None and "mktemp" in word:
                self.scope.temp_names.add(assigned.group(1))
        start = _program_index(words)
        if start is not None:
            name = _program_name(words[start])
            args = words[start + 1 :]
            if name in ("cd", "pushd"):
                # After `cd X || ...` the shell may be in either place.
                failed = segment.separator == "||"
                self.cwd = (
                    None if failed else self.scope.next_cwd(args[0] if args else None, self.cwd)
                )
            elif name == "popd":
                self.cwd = None
            else:
                wrote = wrote or self._program_writes(name, args, stdin)
                if name == "cat" and bodies and segment.separator in PIPES:
                    self.piped = bodies  # `cat <<'EOF' | python3`
        for _ in range(_closed_subshells(segment.tokens)):
            if self.outer:
                self.cwd = self.outer.pop()
        return wrote

    def _program_writes(self, name: str, args: list[str], stdin: list[str]) -> bool:
        if stdin and INTERPRETER.match(name) and interpreter_source(name, args)[0] == "stdin":
            inside = partial(self.scope.inside, cwd=self.cwd)
            return any(code_writes(body, inside) for body in stdin)  # `python3 - <<EOF`
        if name in _SHELLS:
            source, code = shell_source(args)
            scripts = [code] if source == "arg" and code is not None else []
            scripts += stdin if source == "stdin" else []
            return any(self._nested(script) for script in scripts)
        return _program_writes(name, args, self.scope, self.cwd)

    def _nested(self, script: str) -> bool:
        if self.nesting >= _MAX_NESTING:
            return False
        return _Walk(self.scope, self.cwd, self.nesting + 1).command_writes(script)


def _opened_subshells(tokens: tuple[Token, ...]) -> int:
    """`(` that open this segment: `(cd x && make)` starts a subshell."""
    first = next((t for t in tokens if not t.operator), None)
    if first is None or first.quoted:
        return 0
    return len(first.text) - len(first.text.lstrip("("))


def _closed_subshells(tokens: tuple[Token, ...]) -> int:
    """`)` in the last word that close more than the word opened (not `$(pwd)`)."""
    last = next((t for t in reversed(tokens) if not t.operator), None)
    if last is None or last.quoted:
        return 0
    return max(0, last.text.count(")") - last.text.count("("))


def _redirect_pass(
    segment: tuple[Token, ...], scope: _Scope, cwd: str | None
) -> tuple[list[str], bool, list[str]]:
    """Judge the segment's redirections: (other words, verdict, heredoc bodies)."""
    words: list[str] = []
    bodies: list[str] = []
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
        target = segment[i]
        i += 1
        if words and words[-1].isdigit():
            words.pop()  # the descriptor of `2>`
        if token.text in INPUT_REDIRECTS:
            if target.body is not None:
                bodies.append(target.body)
            continue  # `< in`, `<<EOF`, `<<< str`: read, never written
        if token.text == ">&" and (target.text.isdigit() or target.text == "-"):
            continue  # `2>&1`, `>&2`, `>&-`
        wrote = wrote or _output_hits(target.text, scope, cwd)
    return words, wrote, bodies


def _output_hits(target: str, scope: _Scope, cwd: str | None) -> bool:
    """A captured ``.log`` is output, not a file change fs_write could make."""
    return not target.lower().endswith(".log") and scope.inside(target, cwd)


def _program_index(words: list[str]) -> int | None:
    """Index of the command word, past assignments, keywords and wrappers."""
    wrapper: str | None = None
    takes_value = False
    for i, raw in enumerate(words):
        word = raw.lstrip("({")
        if takes_value:
            takes_value = False
            continue  # the value of `env -u NAME`, `xargs -I FILE`
        if not word or word in _KEYWORDS or _ASSIGNMENT.match(word):
            continue
        if wrapper is not None and (
            word.startswith("-") or word[0].isdigit() or not _PROGRAM_WORD.match(word)
        ):
            # A wrapper's own options: `sudo -n`, `timeout 10`, `xargs -I {}`.
            takes_value = word in _WRAPPERS[wrapper]
            continue
        name = _program_name(word)
        if name in _WRAPPERS:
            wrapper = name
            continue
        return i
    return None


def _program_name(word: str) -> str:
    return os.path.basename(word.lstrip("({"))


def _program_writes(name: str, args: list[str], scope: _Scope, cwd: str | None) -> bool:
    if name == "tee":
        return any(_output_hits(arg, scope, cwd) for arg in args if arg and not arg.startswith("-"))
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
        source, code = interpreter_source(name, args)
        return (
            source == "arg"
            and code is not None
            and code_writes(code, partial(scope.inside, cwd=cwd))
        )
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
