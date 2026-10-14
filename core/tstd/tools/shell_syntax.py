"""Shell command text as words and operators, for the write check (TD-5009).

Pure text work for :mod:`tstd.tools.shell_writes`: POSIX-ish words,
unquoted operators, heredoc bodies attached to their delimiter, and simple
commands split at separators.  Nothing here runs a shell.  It is close
enough for a nudge and is not a parser to build security on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

#: The next word is a file the command may write.
OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", ">&", "&>", "&>>"})
#: The next word is read, or is a heredoc delimiter or a here-string.  It
#: is never a write target and never an argument of the command (`tee out
#: < in` writes only `out`).  `<>` opens read-write but is rare enough to
#: read as input.
INPUT_REDIRECTS = frozenset({"<", "<<", "<<-", "<<<", "<&", "<>"})
SEPARATORS = frozenset({"|", "||", "|&", "&", "&&", ";", ";;", "\n"})
PIPES = frozenset({"|", "|&"})
_HEREDOCS = frozenset({"<<", "<<-"})
# Longest first, so `&>>` is not read as `&>` then `>`, nor `<<<` as `<<`.
_OPERATORS = tuple(sorted(OUTPUT_REDIRECTS | INPUT_REDIRECTS | SEPARATORS, key=len, reverse=True))
# An unquoted delimiter must look like one, so `$((1 << 2))` is no heredoc.
_BARE_DELIMITER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Token:
    text: str
    operator: bool = False
    quoted: bool = False
    #: On a heredoc's delimiter word: the lines the heredoc feeds in.
    body: str | None = None


@dataclass(frozen=True)
class Segment:
    """One simple command, and the separator after it ("" at the end)."""

    tokens: tuple[Token, ...]
    separator: str


def tokenize(command: str) -> list[Token] | None:
    """Words and unquoted operators; None on an unclosed quote.

    A heredoc body is cut out at the newline that ends its opening line
    and attached to the delimiter word: it is data, so `>` in a body of
    HTML or Python is not a redirect.  An unterminated body runs to the
    end, as in the shell.  `<<` inside quotes opens nothing.
    """
    tokens: list[Token] = []
    pending: list[tuple[int, bool]] = []  # (delimiter token index, strip tabs)
    buf: list[str] = []
    in_word = False
    quoted = False
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal in_word, quoted
        if in_word:
            word = Token("".join(buf), quoted=quoted)
            opener = tokens[-1] if tokens else None
            if (
                opener is not None
                and opener.operator
                and opener.text in _HEREDOCS
                and (word.quoted or _BARE_DELIMITER.match(word.text))
            ):
                pending.append((len(tokens), opener.text == "<<-"))
            tokens.append(word)
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
                continue
            flush()
            tokens.append(Token(op, operator=True))
            i += len(op)
            if op == "\n" and pending:
                i = _read_bodies(command, i, tokens, pending)
                pending.clear()
    flush()
    return tokens


def _read_bodies(
    command: str, start: int, tokens: list[Token], pending: list[tuple[int, bool]]
) -> int:
    """Attach each pending heredoc's body; return where the shell text resumes."""
    pos = start
    for index, strip_tabs in pending:
        delimiter = tokens[index].text
        lines: list[str] = []
        while pos < len(command):
            end = command.find("\n", pos)
            stop = len(command) if end < 0 else end
            line = command[pos:stop]
            pos = stop + 1
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate.strip() == delimiter:
                break
            lines.append(line)
        tokens[index] = replace(tokens[index], body="\n".join(lines))
    return min(pos, len(command))


def segments(tokens: list[Token]) -> list[Segment]:
    """Simple commands: the tokens between separators, empty ones dropped."""
    found: list[Segment] = []
    current: list[Token] = []
    for token in tokens:
        if token.operator and token.text in SEPARATORS:
            if current:
                found.append(Segment(tuple(current), token.text))
            current = []
        else:
            current.append(token)
    if current:
        found.append(Segment(tuple(current), ""))
    return found
