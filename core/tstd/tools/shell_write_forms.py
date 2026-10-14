"""How particular programs write files, read from their arguments (TD-5009).

Helpers for :mod:`tstd.tools.shell_writes`: the in-place switches of
``sed``, ``perl`` and ``gawk``, where ``python``, ``node`` and ``sh`` take
their code from, and whether that code opens a file for writing.  Pure
text work: nothing here runs or opens anything.  :func:`code_writes`
takes *inside*, the caller's "does this path land in the workspace" test,
so this module does not need to know the workspace.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

#: A program name that runs inline code given on the command line.
INTERPRETER = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy3?|node|nodejs)$")

#: Where a program takes its code: an argument (``-c``/``-e``), standard
#: input (``python3 -``, ``bash -s``, a heredoc), or a file or module whose
#: writes are its own.
CodeSource = Literal["arg", "stdin", "file"]

#: Characters read for one call's arguments.  A real path and mode fit in
#: far less; an unclosed ``open(`` stops here instead of at the end.
_CALL_SCAN_LIMIT = 2_000
#: Characters read for call arguments across one piece of code.  Without
#: it, text full of unclosed ``open(`` is read once per match: quadratic,
#: on the daemon's event loop.  Code that needs more counts as a write.
_CODE_SCAN_BUDGET = 100_000
#: Options of ``sh``/``bash`` whose value is the next word.
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})

_OPEN_CALL = re.compile(r"\bopen\s*\(")
_NODE_WRITE_CALL = re.compile(
    r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream)\s*\("
)
_PATH_WRITE = re.compile(
    r"Path\s*\(\s*[rRuU]?(['\"])([^'\"]*)\1\s*\)\s*\.write_(?:text|bytes)\s*\("
)
_ANY_PATH_WRITE = re.compile(r"\.write_(?:text|bytes)\s*\(")
_FILEINPUT_INPLACE = re.compile(r"\binplace\s*=\s*True\b")
_KEYWORD_ARG = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.DOTALL)
_STRING_LITERAL = re.compile(r"^\s*[rRbBuUfF]{0,2}(['\"`])(.*)\1\s*$", re.DOTALL)
# `p = 'docs/a.md'` (or `const p = "..."`) at the start of a line or statement.
_STRING_ASSIGNMENT = re.compile(
    r"(?:^|;)\s*(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*=\s*[rRuU]?(['\"])([^'\"\n]*)\2",
    re.MULTILINE,
)


def sed_files(args: list[str]) -> list[str] | None:
    """File operands of an in-place ``sed``; None when it is not in place."""
    in_place = script_given = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands.extend(args[i:])
            break
        if arg.startswith("--"):
            key = arg.split("=", 1)[0]
            in_place = in_place or key == "--in-place"
            script_given = script_given or key in ("--expression", "--file")
            if key in ("--expression", "--file", "--line-length") and "=" not in arg:
                i += 1
            continue
        if not arg.startswith("-") or arg == "-":
            operands.append(arg)
            continue
        for pos, flag in enumerate(arg[1:], start=1):
            if flag == "i":  # `-i`, `-i.bak`, `-Ei`; the rest is a suffix
                in_place = True
                if pos == len(arg) - 1 and i < len(args) and args[i] == "":
                    i += 1  # BSD `-i ''`
                break
            if flag in "efl":
                script_given = script_given or flag in "ef"
                if pos == len(arg) - 1:
                    i += 1
                break
    if not in_place:
        return None
    return operands if script_given else operands[1:]


def perl_files(args: list[str]) -> list[str] | None:
    """File operands of an in-place ``perl``; None when it is not in place."""
    in_place = code_given = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands.extend(args[i:])
            break
        if arg.startswith("--"):
            continue
        if not arg.startswith("-") or arg == "-":
            operands.append(arg)
            continue
        body = arg[1:]
        pos = 0
        while pos < len(body):
            flag = body[pos]
            pos += 1
            if flag == "i":  # `-pi`, `-i.bak`, `-0777pi`; the rest is a suffix
                in_place = True
                break
            if flag in "eE":
                code_given = True
                if pos == len(body):
                    i += 1
                break
            if flag in "MmIxCdDFV":
                break  # the rest of the cluster is this switch's value
            if flag == "0" and body[pos : pos + 1] in ("x", "X"):
                pos = _skip_chars(body, pos + 1, string.hexdigits)
            elif flag in "0l":
                pos = _skip_chars(body, pos, string.octdigits)
    if not in_place:
        return None
    return operands if code_given else operands[1:]


def gawk_in_place(args: list[str]) -> bool:
    """``gawk -i inplace`` rewrites its input files."""
    for i, arg in enumerate(args):
        if arg in ("-iinplace", "--include=inplace"):
            return True
        if arg in ("-i", "--include") and i + 1 < len(args) and args[i + 1] == "inplace":
            return True
    return False


def interpreter_source(name: str, args: list[str]) -> tuple[CodeSource, str | None]:
    """Where ``python`` or ``node`` takes its code, and the code when inline."""
    return _node_source(args) if name.startswith("node") else _python_source(args)


def shell_source(args: list[str]) -> tuple[CodeSource, str | None]:
    """Where ``sh``/``bash`` takes its script: ``-c SCRIPT``, stdin, or a file."""
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg in _SHELL_VALUE_OPTIONS:
            i += 1
        elif arg == "--":
            break
        elif arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return "arg", (args[i] if i < len(args) else None)  # `-c`, `-lc`, `-ec`
        elif arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]:
            return "stdin", None
        elif not arg.startswith(("-", "+")):
            return "file", None
    return ("file" if i < len(args) else "stdin"), None


def _python_source(args: list[str]) -> tuple[CodeSource, str | None]:
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "-":
            return "stdin", None
        if not arg.startswith("-"):
            return "file", None
        if arg.startswith("--"):
            continue
        for pos, flag in enumerate(arg[1:], start=1):
            if flag == "c":  # `-c CODE`, `-cCODE`, `-Bc CODE`
                rest = arg[pos + 1 :]
                return "arg", rest or (args[i] if i < len(args) else None)
            if flag == "m":
                return "file", None  # a module: its writes are its own
            if flag in "WX":  # `-W ignore`, `-Xutf8`: the rest is the value
                if pos == len(arg) - 1:
                    i += 1
                break
    return "stdin", None


def _node_source(args: list[str]) -> tuple[CodeSource, str | None]:
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg in ("-e", "--eval", "-p", "--print"):
            return "arg", (args[i] if i < len(args) else None)
        if arg.startswith(("--eval=", "--print=")):
            return "arg", arg.split("=", 1)[1]
        if arg == "-":
            return "stdin", None
        if arg in ("-r", "--require"):
            i += 1
        elif not arg.startswith("-"):
            return "file", None
    return "stdin", None


def code_writes(code: str, inside: Callable[[str], bool]) -> bool:
    """Whether inline Python or JavaScript opens a workspace file for writing.

    A path the code builds at run time cannot be read here and counts; a
    plain string literal, or a name assigned one, is judged by *inside*.
    """
    names = {m.group(1): m.group(3) for m in _STRING_ASSIGNMENT.finditer(code)}
    budget = _ScanBudget(_CODE_SCAN_BUDGET)
    for match in _OPEN_CALL.finditer(code):
        call = _call_arguments(code, match.end(), budget)
        if call is None:
            return True
        writes, path = _open_call(call, names)
        if writes and (path is None or inside(path)):
            return True
    for match in _NODE_WRITE_CALL.finditer(code):
        args = _call_arguments(code, match.end(), budget)
        if args is None:
            return True
        path = _path_of(args[0], names) if args else None
        if path is None or inside(path):
            return True
    literal_writes = _PATH_WRITE.findall(code)
    if len(_ANY_PATH_WRITE.findall(code)) > len(literal_writes):
        return True  # `p.write_text(...)` on a path built at run time
    if any(inside(path) for _, path in literal_writes):
        return True
    return bool(_FILEINPUT_INPLACE.search(code))


def _skip_chars(text: str, pos: int, allowed: str) -> int:
    while pos < len(text) and text[pos] in allowed:
        pos += 1
    return pos


def _open_call(args: list[str], names: dict[str, str]) -> tuple[bool, str | None]:
    """(opens for writing, the path when it can be read from the text)."""
    positional: list[str] = []
    named: dict[str, str] = {}
    for arg in args:
        keyword = _KEYWORD_ARG.match(arg)
        if keyword is None:
            positional.append(arg)
        else:
            named[keyword.group(1)] = keyword.group(2)
    mode_src = named.get("mode", positional[1] if len(positional) > 1 else None)
    file_src = named.get("file", positional[0] if positional else None)
    mode = _literal(mode_src) if mode_src is not None else None
    writes = mode is not None and any(flag in mode for flag in "wax+")
    return writes, _path_of(file_src, names) if file_src is not None else None


def _path_of(arg: str, names: dict[str, str]) -> str | None:
    if arg.strip() == "os.devnull":
        return "/dev/null"
    literal = _literal(arg)
    return literal if literal is not None else names.get(arg.strip())


def _literal(arg: str) -> str | None:
    match = _STRING_LITERAL.match(arg)
    return match.group(2) if match is not None else None


@dataclass
class _ScanBudget:
    """Characters :func:`_call_arguments` may still read in one piece of code."""

    left: int


def _call_arguments(code: str, start: int, budget: _ScanBudget) -> list[str] | None:
    """Top-level arguments of the call whose ``(`` ends just before *start*.

    Reads at most :data:`_CALL_SCAN_LIMIT` characters; None once *budget*
    is spent, so the caller can stop reading.
    """
    if budget.left <= 0:
        return None
    reach = min(len(code), start + _CALL_SCAN_LIMIT)
    stop = min(reach, start + budget.left)
    args: list[str] = []
    current: list[str] = []
    depth = 0
    i = start
    while i < stop:
        ch = code[i]
        if ch in "'\"`":
            end = i + 1
            while end < stop and code[end] != ch:
                end += 2 if code[end] == "\\" else 1
            current.append(code[i : end + 1])
            i = end + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    budget.left -= i - start
    if stop < reach and i >= stop:
        return None  # the budget ran out inside this call
    tail = "".join(current).strip()
    if tail:
        args.append(tail)
    return args
