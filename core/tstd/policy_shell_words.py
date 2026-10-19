"""Words of one shell command, as policy reads them (TD-5101).

The scanner in :mod:`tstd.policy_shell` finds the commands of a line; this
module reads the words of one: which wrappers come off before an ``auto``
rule is matched (only those that never change what runs), and the readings
``ask`` and ``never`` rules are checked against (every wrapper, keyword,
assignment and redirect peeled off in turn).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_PEELS = 16  # deny readings kept per command
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A redirect word: an optional fd, the operator, and maybe its target.
_REDIRECT = re.compile(r"^[0-9]*(?:>>|>&|>\||<<-|<<<|<<|<&|<>|&>>|&>|>|<)(.*)$", re.S)
_KEYWORDS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "fi", "done", "esac"}
    | {"coproc"}
)
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"})


def unquote(word: str, posix: bool) -> str:
    """The word as the program sees it, roughly: quotes and escapes removed."""
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if (posix and c == "\\") or (not posix and c == "^"):
            if not word.startswith("\\\n", i):
                out.append(word[i + 1 : i + 2])
            i += 2
        elif posix and c == "'":
            end = word.find("'", i + 1)
            end = n if end < 0 else end
            out.append(word[i + 1 : end])
            i = end + 1
        elif c == '"':
            j = i + 1
            while j < n and word[j] != '"':
                if posix and word[j] == "\\" and word[j + 1 : j + 2] in ('"', "\\", "$", "`"):
                    j += 1
                out.append(word[j])
                j += 1
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def basename(word: str) -> str:
    return word.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def dash_c_script(args: list[str]) -> str | None:
    """The script of ``sh -c 'script'`` (after options), or None."""
    has_c = False
    j = 0
    while j < len(args) and args[j][:1] in "-+" and args[j] not in ("-", "--"):
        flag = args[j]
        if flag in ("-o", "+o"):
            j += 2
            continue
        has_c = has_c or (not flag.startswith("--") and "c" in flag[1:])
        j += 1
    if j < len(args) and args[j] == "--":
        j += 1
    return args[j] if has_c and j < len(args) else None


@dataclass(frozen=True)
class _Opts:
    """A wrapper's options: which take a value, and how many operands
    (``timeout``'s duration) come before the wrapped command."""

    short_value: str = ""
    short_flag: str = ""
    long_value: frozenset[str] = frozenset()
    long_flag: frozenset[str] = frozenset()
    operands: int = 0
    numeric: bool = False  # `nice -10`


# Wrappers taken off before an ``auto`` rule is matched: they change how long
# or how nicely a command runs, never what it runs. Unknown options stop it.
_ALLOW_WRAPPERS: dict[str, _Opts] = {
    "timeout": _Opts(
        short_value="sk",
        short_flag="v",
        long_value=frozenset({"signal", "kill-after"}),
        long_flag=frozenset({"preserve-status", "foreground", "verbose"}),
        operands=1,
    ),
    "nice": _Opts(short_value="n", long_value=frozenset({"adjustment"}), numeric=True),
    "nohup": _Opts(),
    "time": _Opts(short_flag="p"),
    "stdbuf": _Opts(short_value="ioe", long_value=frozenset({"input", "output", "error"})),
}

# Wrappers peeled before ``ask`` and ``never`` rules are checked, leniently:
# here reading too much only makes a refusal more likely.
_DENY_WRAPPERS: dict[str, _Opts] = {
    **_ALLOW_WRAPPERS,
    "time": _Opts(short_value="fo", long_value=frozenset({"format", "output"})),
    "env": _Opts(short_value="uCS", long_value=frozenset({"unset", "chdir", "split-string"})),
    "sudo": _Opts(
        short_value="CDghpRrTtUu",
        long_value=frozenset(
            {"chdir", "chroot", "close-from", "command-timeout", "group", "host"}
            | {"other-user", "prompt", "role", "type", "user"}
        ),
    ),
    "doas": _Opts(short_value="Cu"),
    "xargs": _Opts(
        short_value="aEdILnPs",
        long_value=frozenset(
            {"arg-file", "delimiter", "eof", "max-args", "max-chars", "max-lines"}
            | {"max-procs", "process-slot-var", "replace"}
        ),
    ),
    "exec": _Opts(short_value="a"),
    "command": _Opts(),
    "builtin": _Opts(),
}


def _skip_options(words: list[str], k: int, opts: _Opts, *, strict: bool) -> int | None:
    """Index of the wrapped command after the wrapper at *k*.

    Strict: None on any option it does not know, or when no command follows.
    """
    j = k + 1
    while j < len(words) and words[j].startswith("-") and words[j] != "-":
        word = words[j]
        if word == "--":
            j += 1
            break
        if word.startswith("--"):
            name, eq, _ = word[2:].partition("=")
            if name in opts.long_value:
                j += 1 if eq else 2
            elif name in opts.long_flag or not strict:
                j += 1
            else:
                return None
            continue
        if opts.numeric and word[1:].isdigit():
            j += 1
            continue
        step = 1
        for index, letter in enumerate(word[1:]):
            if letter in opts.short_value:
                step = 1 if index + 2 < len(word) else 2  # attached value, or the next word
                break
            if letter not in opts.short_flag and strict:
                return None
        j += step
    j += opts.operands
    if j >= len(words):
        return None if strict else len(words)
    return j


def _peel_once(words: list[str], k: int) -> int | None:
    """Past one keyword, assignment, redirect or wrapper at *k*, or None."""
    word = words[k]
    if word in _KEYWORDS or _ASSIGNMENT.match(word):
        return k + 1
    if word == "function":
        return k + 2  # `function name { body`: the body is the command
    redirect = _REDIRECT.match(word)
    if redirect is not None:
        return k + 1 if redirect.group(1) else k + 2
    opts = _DENY_WRAPPERS.get(basename(word))
    return None if opts is None else _skip_options(words, k, opts, strict=False)


def peel_all(words: list[str]) -> int:
    k = 0
    while k < len(words):
        nxt = _peel_once(words, k)
        if nxt is None:
            break
        k = nxt
    return k


def allow_start(raw: list[str]) -> int:
    """Index of the first word an ``auto`` rule is matched from.

    Only a bare ``timeout``, ``nice``, ``nohup``, ``time`` or ``stdbuf``
    with options it knows comes off; anything else stops the peeling.
    """
    k = 0
    while k < len(raw):
        opts = _ALLOW_WRAPPERS.get(raw[k])
        nxt = None if opts is None else _skip_options(raw, k, opts, strict=True)
        if nxt is None:
            break
        k = nxt
    return k


def deny_readings(words: list[str]) -> list[str]:
    """The unquoted command, then again after each peel."""
    starts = [0]
    while starts[-1] < len(words):
        nxt = _peel_once(words, starts[-1])
        if nxt is None:
            break
        starts.append(nxt)
    # A long run of assignments would cost a join per word; the first few
    # readings and the fully peeled one are what rules can name.
    starts = starts[:_MAX_PEELS] + starts[-1:]
    return [" ".join(words[k:]) for k in starts]
