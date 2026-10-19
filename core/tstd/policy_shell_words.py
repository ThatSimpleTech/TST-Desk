"""Words of one shell command, as policy reads them (TD-5101, TD-5156).

The scanner in :mod:`tstd.policy_shell` finds the commands of a line; this
module reads the words of one: which wrappers come off before an ``auto``
rule is matched (only those that never change what runs), the readings
``ask`` and ``never`` rules are checked against (every wrapper, keyword,
assignment and redirect peeled off in turn, and the program without its
folder), and the strings the command runs as commands (``eval``, ``sh -c``,
``cmd /c``, an ``alias`` value, a ``trap`` action).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Literal

Dialect = Literal["posix", "cmd"]

_MAX_PEELS = 16  # deny readings kept per command
_MAX_BRACE_WORDS = 64  # words one brace expansion yields for a deny reading
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A redirect word: an optional fd, the operator, and maybe its target.
_REDIRECT = re.compile(r"^[0-9]*(?:>>|>&|>\||<<-|<<<|<<|<&|<>|&>>|&>|>|<)(.*)$", re.S)
_KEYWORDS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "fi", "done", "esac"}
    | {"coproc"}
)
# Compound commands `coproc NAME` can run, besides the keywords above.
_COMPOUND = frozenset({"for", "case", "select", "[["})
# bash's `{1..9}`, `{a..e..2}`; the digits are capped so int() stays cheap.
_SEQUENCE = re.compile(
    r"(-?[0-9]{1,18})\.\.(-?[0-9]{1,18})(?:\.\.(-?[0-9]{1,18}))?"
    r"|([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?[0-9]{1,18}))?"
)
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"})
_RUNNABLE_EXTENSIONS = frozenset({"exe", "com", "bat", "cmd"})


class Incomplete(Exception):
    """The text cannot be split with confidence."""


def unquote(word: str, posix: bool) -> str:
    """The word as the program sees it, roughly: quotes and escapes removed."""
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if (posix and c == "\\") or (not posix and c == "^"):
            if not word.startswith("\\\n", i):
                # A backslash that ends the text is itself.
                out.append(word[i + 1 : i + 2] or ("\\" if posix else ""))
            i += 2
        elif posix and c == "'":
            end = word.find("'", i + 1)
            end = n if end < 0 else end
            out.append(word[i + 1 : end])
            i = end + 1
        elif c == '"':
            j = i + 1
            while j < n and word[j] != '"':
                if posix and word.startswith("\\\n", j):
                    j += 2
                    continue
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


def run_strings(words: list[str], posix: bool) -> list[tuple[str, Dialect]]:
    """The strings a command runs as commands, and the dialect they run in.

    ``eval``, ``sh -c`` and ``cmd /c`` run theirs now, an ``alias`` value runs
    where the alias is used on a later line, and a ``trap`` action at exit.
    """
    k = peel_all(words)
    if k >= len(words):
        return []
    name, rest = basename(words[k]), words[k + 1 :]
    if name.lower() in ("cmd", "cmd.exe") and rest and rest[0].lower() in ("/c", "/k"):
        return [(" ".join(rest[1:]), "cmd")]
    if not posix:
        if name.lower() == "for" and any(w.lower().startswith("/f") for w in rest):
            # `for /f … in ('cmd')`, or (`cmd`) with usebackq, runs the
            # quoted command; read whole it is cut at the quotes.
            raise Incomplete
        return []
    if name == "eval" and rest:
        return [(" ".join(rest), "posix")]
    script = dash_c_script(rest) if name in SHELLS else None
    if script is not None:
        return [(script, "posix")]
    if name == "alias":
        return [(w.partition("=")[2], "posix") for w in rest if "=" in w]
    if name == "trap" and len(rest) > 1:
        return [(rest[1] if rest[0] == "--" else rest[0], "posix")]
    return []


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
    if word == "coproc" and k + 2 < len(words):
        after = words[k + 2]
        # `coproc NAME { cmd; }`: NAME names the job, cmd is the command.
        return k + 2 if after in _KEYWORDS or after in _COMPOUND else k + 1
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


def deny_readings(words: list[str], *, braces: bool = True) -> list[str]:
    """The unquoted command, then again after each peel; each also with
    its program named without its folder (``/usr/bin/curl`` → ``curl``),
    and all of it again after bash's brace expansion (``{curl,x}``)."""
    starts = [0]
    while starts[-1] < len(words):
        nxt = _peel_once(words, starts[-1])
        if nxt is None:
            break
        starts.append(nxt)
    # A long run of assignments would cost a join per word; the first few
    # readings and the fully peeled one are what rules can name.
    starts = starts[:_MAX_PEELS] + starts[-1:]
    readings: list[str] = []
    for k in starts:
        readings.append(" ".join(words[k:]))
        if k < len(words) and (name := program_name(words[k])) != words[k]:
            readings.append(" ".join([name, *words[k + 1 :]]))
    if braces:
        expanded = [x for w in words for x in (brace_words(w) or [w])]
        if expanded != words:
            readings.extend(deny_readings(expanded, braces=False))
    return readings


def brace_words(word: str) -> list[str] | None:
    """The words bash's brace expansion makes of *word* (``{curl,x}`` →
    ``curl``, ``x``), at most 64, or None when it has none. dash and ash
    take the word as written, so a braced program name is read both ways."""
    words = _expand_braces(word)
    return None if words == [word] else words


def _expand_braces(word: str) -> list[str]:
    group = _brace_group(word)
    if group is None:
        return [word]
    start, end, alternatives = group
    out: list[str] = []
    for alternative in alternatives:
        # Each round removes a pair of braces, so this ends.
        out.extend(_expand_braces(word[:start] + alternative + word[end:]))
        if len(out) >= _MAX_BRACE_WORDS:
            break
    return out[:_MAX_BRACE_WORDS]


def _brace_group(word: str) -> tuple[int, int, list[str]] | None:
    """The first ``{a,b}`` or ``{1..3}`` in *word*: its span and its parts."""
    for start, c in enumerate(word):
        if c != "{" or word[start - 1 : start] == "$":
            continue
        depth, parts, last = 0, [], start + 1
        for end in range(start + 1, len(word)):
            ch = word[end]
            if ch == "{":
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
            elif ch == "}":
                parts.append(word[last:end])
                found = parts if len(parts) > 1 else _sequence(parts[0])
                if found is not None:
                    return start, end + 1, found
                break
            elif ch == "," and not depth:
                parts.append(word[last:end])
                last = end + 1
    return None


def _sequence(text: str) -> list[str] | None:
    m = _SEQUENCE.fullmatch(text)
    if m is None:
        return None
    numbers = m.group(1) is not None
    first, last = (int(m[1]), int(m[2])) if numbers else (ord(m[4]), ord(m[5]))
    step = abs(int((m[3] if numbers else m[6]) or 1)) or 1
    span = range(first, last + 1, step) if first <= last else range(first, last - 1, -step)
    return [str(x) if numbers else chr(x) for x in itertools.islice(span, _MAX_BRACE_WORDS)]


def program_name(word: str) -> str:
    """*word* as a rule names the program: no folder, and on Windows no
    ``.exe``-style extension (``C:\\Tools\\curl.EXE`` → ``curl``)."""
    name = basename(word)
    stem, dot, ext = name.rpartition(".")
    return stem if dot and stem and ext.lower() in _RUNNABLE_EXTENSIONS else name
