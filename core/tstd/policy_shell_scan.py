"""The scanner behind :mod:`tstd.policy_shell` (TD-5101, TD-5156).

Walks a command line the way ``sh`` (or ``cmd``) reads it and records each
simple command's span and words. Quotes, escapes, comments, line
continuations and heredoc bodies are honoured, because a quote the scanner
misreads could hide a separator the shell sees. Constructs where shells
disagree raise ``Incomplete`` instead of guessing; the differential test
in ``tests/test_policy_shell_differential.py`` checks that against the
shells themselves.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from .policy_shell_heredoc import Heredoc, body_end, read_operator, skip_splices, unsplice
from .policy_shell_words import Dialect, Incomplete, run_strings, unquote

__all__ = ["Dialect", "Scanner", "Seg"]

# Deep nesting is either an attack on the parser or not worth reading, and
# the caps keep the recursion far from Python's limit.
_MAX_DEPTH = 4  # scanners inside scanners: backticks, bodies, eval, sh -c
_MAX_NEST = 16  # $( and ${ inside one another, within one scanner
_ROUGH_SEPARATOR = re.compile(r"[^;&|\n()`]+")
_WORD = re.compile(r"[^\s\"']+")
# An operator split by `\`-newline: the shell joins the halves (`&` + `&`).
_JOINS = {"<": "<>&|(", ">": "<>&|(", "&": "&>|", "|": "|&", ";": ";&", "(": "("}


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
        self._heredocs: list[Heredoc] = []
        self._subs = 0  # how many $( … ) we are inside
        self._nest = 0  # how many $( and ${ we are inside

    def run(self) -> None:
        try:
            if self.depth > _MAX_DEPTH:
                raise Incomplete
            self._list(None)
            self._unwrap_own()
        except Incomplete:
            self.complete = False
            # No auto rule applies now, so over-splitting costs nothing: cut
            # the whole text at every operator, so ask and never rules still
            # see each command in it, and the string given to eval or sh -c.
            # Cut it again with `\`-newline removed, which may join a word.
            self._rough(self.text)
            if self.posix and "\\\n" in self.text:
                self._rough(self.text.replace("\\\n", ""))

    def _rough(self, text: str) -> None:
        for piece in _ROUGH_SEPARATOR.finditer(text):
            words = [(m.start(), m.end()) for m in _WORD.finditer(text, *piece.span())]
            if not words:
                continue
            seg = Seg(text, piece.start(), piece.end(), words, [])
            self.segs.append(seg)
            if self.depth < _MAX_DEPTH:
                # Incomplete already; what the unwrap read is kept.
                with contextlib.suppress(Incomplete):
                    self._unwrap(seg)

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
            elif posix and text.startswith("\\\n", i):
                self._splice()
            elif (posix and c == "\\") or (not posix and c == "^"):
                self._open_word(frame, i)
                self.i += 2
            elif posix and c == "'":
                self._open_word(frame, i)
                end = text.find("'", i + 1)
                if end < 0:
                    raise Incomplete
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
                    raise Incomplete  # bash arithmetic or two subshells: shells disagree
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
            raise Incomplete  # an unclosed $(
        self._close_seg(frame, self.n)

    def _splice(self) -> None:
        """Skip ``\\``-newline, which the shell removes before it reads words.

        A comment can start right after it, and the characters either side
        are read as one, so where they would join into an operator the line
        is too odd to read with confidence.
        """
        start, end = self.i, skip_splices(self.text, self.i)
        after = self.text[end : end + 1]
        if after and after in _JOINS.get(self.text[start - 1 : start], ""):
            raise Incomplete
        self.i = end

    def _new_frame(self) -> _Frame:
        return _Frame(self.i)

    def _open_word(self, frame: _Frame, i: int) -> None:
        if frame.word_start is None:
            frame.word_start = i

    def _close_word(self, frame: _Frame, i: int) -> None:
        if frame.word_start is None:
            return
        if self._subs and self.text[frame.word_start : i].replace("\\\n", "") == "case":
            raise Incomplete  # `pattern)` would close the $( early
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
            raise Incomplete

    def _sub(self) -> None:
        self._enter()
        self._subs += 1
        # A heredoc opened before the `$(` has its body after the line, not
        # after a newline inside the substitution.
        outside, self._heredocs = self._heredocs, []
        self._list(")")
        if self._heredocs:
            # A body still owed when `)` closes: bash reads it from the lines
            # after the substitution, dash leaves it empty and runs them.
            raise Incomplete
        self._heredocs = outside
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
        raise Incomplete

    def _dollar(self, *, quoted: bool) -> None:
        text, i = self.text, self.i
        if text.startswith("$\\\n", i):
            raise Incomplete  # the shell joins them: `$` + `(` is a substitution
        if text.startswith("$((", i):
            raise Incomplete  # bash falls back to $( ( … ) ) when it isn't arithmetic
        if not quoted and text.startswith("$'", i):
            raise Incomplete  # bash reads `\'` inside; dash ends at it
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
                raise Incomplete  # quoting inside ${ } differs between shells
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
        raise Incomplete

    def _backtick(self, *, in_dquote: bool) -> None:
        j = self.i + 1
        while j < self.n and self.text[j] != "`":
            j += 2 if self.text[j] == "\\" else 1
        if j >= self.n:
            raise Incomplete
        escapes = r"\\`$\"" if in_dquote else r"\\`$"
        inner = re.sub(rf"\\([{escapes}])", r"\1", self.text[self.i + 1 : j])
        self.i = j + 1
        self._nested(inner, self.dialect)

    def _heredoc(self, frame: _Frame) -> None:
        doc, self.i = read_operator(self.text, self.i, frame.bodies)
        self._heredocs.append(doc)

    def _read_bodies(self) -> None:
        """Consume the heredoc bodies that start after this newline.

        Each body, with its closing line, joins the segment that opened it,
        so an ``auto`` rule must match the data too. An unquoted body's
        substitutions run, so they are read as commands.
        """
        pending, self._heredocs = self._heredocs, []
        for doc in pending:
            start = self.i
            end, resume = body_end(self.text, start, doc, in_sub=self._subs > 0)
            doc.bodies.append(self.text[start:resume].rstrip("\n"))
            if not doc.quoted:
                self._body(self.text[start:end])
            self.i = resume

    def _body(self, body: str) -> None:
        """Read an unquoted body's substitutions.

        bash drops the body's `\\`-newlines before it reads them, so a
        comment can swallow the next line; dash and ash keep them. Both
        readings count, and where they differ the line is incomplete.
        """
        joined = unsplice(body)
        found = self._nested(body, self.dialect, body=True)
        if joined != body and self._nested(joined, self.dialect, body=True) != found:
            raise Incomplete

    def _nested(self, text: str, dialect: Dialect, *, body: bool = False) -> list[list[str]]:
        """Scan *text* as commands (or a heredoc body), keep its segments,
        and return their words."""
        inner = Scanner(text, dialect, self.depth + 1)
        if body:
            inner.run_body()
        else:
            inner.run()
        for seg in inner.segs:
            seg.foreign = True
        self.segs.extend(inner.segs)
        if not inner.complete:
            raise Incomplete
        return [[unquote(w, dialect == "posix") for w in seg.raw_words()] for seg in inner.segs]

    def run_body(self) -> None:
        """Read an unquoted heredoc body: data, except its substitutions."""
        try:
            if self.depth > _MAX_DEPTH:
                raise Incomplete
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
        except Incomplete:
            self.complete = False

    def _unwrap_own(self) -> None:
        for seg in [s for s in self.segs if not s.foreign]:
            self._unwrap(seg)

    def _unwrap(self, seg: Seg) -> None:
        """Read the strings the command runs (``eval``, ``sh -c``, …) as commands."""
        words = [unquote(w, self.posix) for w in seg.raw_words()]
        for script, dialect in run_strings(words, self.posix):
            self._nested(script, dialect)
