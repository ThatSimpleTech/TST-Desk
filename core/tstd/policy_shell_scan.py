"""The scanner behind :mod:`tstd.policy_shell` (TD-5101).

Walks a command line the way ``sh`` (or ``cmd``) reads it and records each
simple command's span and words. Quotes, escapes, comments and heredoc
bodies are honoured, because a quote the scanner misreads could hide a
separator the shell sees. Constructs where shells disagree raise
``_Incomplete`` instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .policy_shell_words import SHELLS, basename, dash_c_script, peel_all, unquote

Dialect = Literal["posix", "cmd"]

# Deep nesting is either an attack on the parser or not worth reading, and
# the caps keep the recursion far from Python's limit.
_MAX_DEPTH = 4  # scanners inside scanners: backticks, bodies, eval, sh -c
_MAX_NEST = 16  # $( and ${ inside one another, within one scanner
_DELIMITER_STOP = frozenset(" \t\n;&|<>()")
_ROUGH_SEPARATOR = re.compile(r"[^;&|\n()`]+")
_WORD = re.compile(r"[^\s\"']+")


class _Incomplete(Exception):
    """The text cannot be split with confidence."""


@dataclass
class Seg:
    """A simple command: its span in ``source``, its words, heredoc bodies."""

    source: str
    start: int
    end: int
    words: list[tuple[int, int]]
    bodies: list[str]
    #: Read by an inner scanner, which already unwrapped it.
    foreign: bool = False

    @property
    def text(self) -> str:
        return self.source[self.start : self.end].strip()

    def raw_words(self) -> list[str]:
        return [self.source[s:e] for s, e in self.words]


@dataclass
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    quoted: bool
    bodies: list[str]


@dataclass
class _Frame:
    """The segment being read in one list (top level or one substitution)."""

    start: int
    words: list[tuple[int, int]] = field(default_factory=list)
    word_start: int | None = None
    comment_at: int | None = None
    bodies: list[str] = field(default_factory=list)


class Scanner:
    def __init__(self, text: str, dialect: Dialect, depth: int) -> None:
        self.text = text
        self.n = len(text)
        self.i = 0
        self.posix = dialect == "posix"
        self.dialect: Dialect = dialect
        self.depth = depth
        self.segs: list[Seg] = []
        self.complete = True
        self._heredocs: list[_Heredoc] = []
        self._subs = 0  # how many $( … ) we are inside
        self._nest = 0  # how many $( and ${ we are inside

    def run(self) -> None:
        try:
            if self.depth > _MAX_DEPTH:
                raise _Incomplete
            self._list(None)
            self._unwrap_own()
        except _Incomplete:
            self.complete = False
            # No auto rule applies now, so over-splitting costs nothing: cut
            # the whole text at every operator, so ask and never rules still
            # see each command in it.
            for piece in _ROUGH_SEPARATOR.finditer(self.text):
                words = [(m.start(), m.end()) for m in _WORD.finditer(self.text, *piece.span())]
                if words:
                    self.segs.append(Seg(self.text, piece.start(), piece.end(), words, []))

    # A list of commands, up to the end of text or the ``)`` closing a $( .
    def _list(self, closer: str | None) -> None:
        frame = _Frame(self.i)
        paren_depth = 0
        redirect_end = -1
        text, posix = self.text, self.posix
        while self.i < self.n:
            i = self.i
            c = text[i]
            if c in " \t":
                self._close_word(frame, i)
                self.i += 1
            elif c == "\n":
                self._close_seg(frame, i)
                self.i += 1
                if self._heredocs:
                    self._read_bodies()
                frame = self._new_frame()
            elif posix and c == "#" and frame.word_start is None:
                frame.comment_at = i if frame.comment_at is None else frame.comment_at
                end = text.find("\n", i)
                self.i = self.n if end < 0 else end
            elif (posix and c == "\\") or (not posix and c == "^"):
                self._open_word(frame, i)
                self.i += 2
            elif posix and c == "'":
                self._open_word(frame, i)
                end = text.find("'", i + 1)
                if end < 0:
                    raise _Incomplete
                self.i = end + 1
            elif c == '"':
                self._open_word(frame, i)
                self._dquote()
            elif posix and c == "$":
                self._open_word(frame, i)
                self._dollar(quoted=False)
            elif posix and c == "`":
                self._open_word(frame, i)
                self._backtick(in_dquote=False)
            elif posix and c in "<>" and text.startswith("(", i + 1):
                self._open_word(frame, i)  # process substitution
                self.i += 2
                self._sub()
            elif posix and text.startswith("<<<", i):
                # A here-string. Taken whole, so its second `<<` is not read
                # as a heredoc that would swallow the lines after it.
                self._open_word(frame, i)
                self.i += 3
                redirect_end = self.i
            elif posix and text.startswith("<<", i):
                self._close_word(frame, i)
                self._open_word(frame, i)
                self._heredoc(frame)
            elif c == "(":
                if posix and text.startswith("((", i):
                    raise _Incomplete  # bash arithmetic or two subshells: shells disagree
                self._close_seg(frame, i)
                self.i += 1
                paren_depth += 1
                frame = self._new_frame()
            elif c == ")":
                self._close_seg(frame, i)
                self.i += 1
                if closer == ")" and paren_depth == 0:
                    return
                paren_depth = max(0, paren_depth - 1)
                frame = self._new_frame()
            elif c in ";|" or (c == "&" and redirect_end != i):
                # `&` right after `>` or `<` is a dup (`2>&1`), not a separator.
                self._close_seg(frame, i)
                self.i += 1
                frame = self._new_frame()
            else:
                self._open_word(frame, i)
                self.i += 1
                if c in "<>":
                    redirect_end = self.i
        if closer is not None:
            raise _Incomplete  # an unclosed $(
        self._close_seg(frame, self.n)

    def _new_frame(self) -> _Frame:
        return _Frame(self.i)

    def _open_word(self, frame: _Frame, i: int) -> None:
        if frame.word_start is None:
            frame.word_start = i

    def _close_word(self, frame: _Frame, i: int) -> None:
        if frame.word_start is None:
            return
        if self._subs and self.text[frame.word_start : i] == "case":
            raise _Incomplete  # `pattern)` would close the $( early
        frame.words.append((frame.word_start, i))
        frame.word_start = None

    def _close_seg(self, frame: _Frame, i: int) -> None:
        self._close_word(frame, i)
        if frame.words:
            end = i if frame.comment_at is None else frame.comment_at
            self.segs.append(Seg(self.text, frame.start, end, frame.words, frame.bodies))

    def _enter(self) -> None:
        self._nest += 1
        if self._nest > _MAX_NEST:
            raise _Incomplete

    def _sub(self) -> None:
        self._enter()
        self._subs += 1
        self._list(")")
        self._subs -= 1
        self._nest -= 1

    def _dquote(self) -> None:
        self.i += 1
        while self.i < self.n:
            c = self.text[self.i]
            if c == '"':
                self.i += 1
                return
            if not self.posix and c == "\n":
                break  # cmd quotes end with the line
            if self.posix and c == "\\":
                self.i += 2
            elif self.posix and c == "$":
                self._dollar(quoted=True)
            elif self.posix and c == "`":
                self._backtick(in_dquote=True)
            else:
                self.i += 1
        raise _Incomplete

    def _dollar(self, *, quoted: bool) -> None:
        text, i = self.text, self.i
        if text.startswith("$((", i):
            raise _Incomplete  # bash falls back to $( ( … ) ) when it isn't arithmetic
        if not quoted and text.startswith("$'", i):
            raise _Incomplete  # bash reads `\'` inside; dash ends at it
        if text.startswith("$(", i):
            self.i += 2
            self._sub()
        elif text.startswith("${", i):
            self._brace()
        else:
            self.i += 1

    def _brace(self) -> None:
        self._enter()
        self.i += 2
        depth = 1
        while self.i < self.n:
            c = self.text[self.i]
            if c == "\\":
                self.i += 2
            elif c in "'\"":
                raise _Incomplete  # quoting inside ${ } differs between shells
            elif c == "$":
                self._dollar(quoted=True)
            elif c == "`":
                self._backtick(in_dquote=True)
            else:
                self.i += 1
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        self._nest -= 1
                        return
        raise _Incomplete

    def _backtick(self, *, in_dquote: bool) -> None:
        j = self.i + 1
        while j < self.n and self.text[j] != "`":
            j += 2 if self.text[j] == "\\" else 1
        if j >= self.n:
            raise _Incomplete
        escapes = r"\\`$\"" if in_dquote else r"\\`$"
        inner = re.sub(rf"\\([{escapes}])", r"\1", self.text[self.i + 1 : j])
        self.i = j + 1
        self._nested(inner, self.dialect)

    def _heredoc(self, frame: _Frame) -> None:
        self.i += 2
        strip_tabs = self.text.startswith("-", self.i)
        if strip_tabs:
            self.i += 1
        while self.i < self.n and self.text[self.i] in " \t":
            self.i += 1
        parts: list[str] = []
        quoted = False
        while self.i < self.n and self.text[self.i] not in _DELIMITER_STOP:
            c = self.text[self.i]
            if c in "$`":
                raise _Incomplete
            if c == "\\":
                quoted = True
                parts.append(self.text[self.i + 1 : self.i + 2])
                self.i += 2
            elif c in "'\"":
                end = self.text.find(c, self.i + 1)
                if end < 0:
                    raise _Incomplete
                quoted = True
                parts.append(self.text[self.i + 1 : end])
                self.i = end + 1
            else:
                parts.append(c)
                self.i += 1
        delimiter = "".join(parts)
        if not delimiter:
            raise _Incomplete
        self._heredocs.append(_Heredoc(delimiter, strip_tabs, quoted, frame.bodies))

    def _read_bodies(self) -> None:
        """Consume the heredoc bodies that start after this newline.

        Each body, with its closing line, joins the segment that opened it,
        so an ``auto`` rule must match the data too. An unquoted body's
        substitutions run, so they are read as commands.
        """
        pending, self._heredocs = self._heredocs, []
        for doc in pending:
            start = pos = self.i
            body_end = resume = self.n
            while pos < self.n:
                newline = self.text.find("\n", pos)
                stop = self.n if newline < 0 else newline
                line = self.text[pos:stop]
                if (line.lstrip("\t") if doc.strip_tabs else line) == doc.delimiter:
                    body_end, resume = pos, stop + 1 if newline >= 0 else self.n
                    break
                pos = stop + 1
            doc.bodies.append(self.text[start:resume].rstrip("\n"))
            if not doc.quoted:
                self._nested(self.text[start:body_end], self.dialect, body=True)
            self.i = resume

    def _nested(self, text: str, dialect: Dialect, *, body: bool = False) -> None:
        inner = Scanner(text, dialect, self.depth + 1)
        if body:
            inner.run_body()
        else:
            inner.run()
        for seg in inner.segs:
            seg.foreign = True
        self.segs.extend(inner.segs)
        if not inner.complete:
            raise _Incomplete

    def run_body(self) -> None:
        """Read an unquoted heredoc body: data, except its substitutions."""
        try:
            if self.depth > _MAX_DEPTH:
                raise _Incomplete
            while self.i < self.n:
                c = self.text[self.i]
                if c == "\\":
                    self.i += 2
                elif c == "$":
                    self._dollar(quoted=True)
                elif c == "`":
                    self._backtick(in_dquote=False)
                else:
                    self.i += 1
            self._unwrap_own()
        except _Incomplete:
            self.complete = False

    def _unwrap_own(self) -> None:
        for seg in [s for s in self.segs if not s.foreign]:
            self._unwrap(seg)

    def _unwrap(self, seg: Seg) -> None:
        """Read the string given to ``eval``, ``sh -c`` or ``cmd /c`` as commands."""
        words = [unquote(w, self.posix) for w in seg.raw_words()]
        k = peel_all(words)
        if k >= len(words):
            return
        name = basename(words[k])
        rest = words[k + 1 :]
        if name == "eval" and rest:
            self._nested(" ".join(rest), "posix")
        elif name in SHELLS:
            script = dash_c_script(rest)
            if script is not None:
                self._nested(script, "posix")
        elif name.lower() in ("cmd", "cmd.exe") and rest and rest[0].lower() in ("/c", "/k"):
            self._nested(" ".join(rest[1:]), "cmd")
