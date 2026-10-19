"""The shell splitter fails closed where shells split a line differently (TD-5156).

``split_command`` decides which commands of a line an ``auto`` rule must
cover and which a ``never`` rule is checked against. TD-5101's review found
texts it called completely split while bash, the ``/bin/sh`` of macOS,
Fedora and Arch, read their line breaks differently, so a command on a later
line ran unseen by both kinds of rule.

This test runs a generated corpus (``tests/shell_corpus.py``: harmless
builtins, keywords and ``echo <marker> >>o``) under every shell on the host
(bash, ``bash --posix``, dash, ``sh`` and busybox ash) and compares what ran
with the commands the splitter reports:

- every ``echo`` a shell ran, with the arguments it ran with, is one of the
  splitter's commands, or the split is incomplete, so no ``auto`` rule can
  apply to the line
- a ``never`` rule naming any command that ran refuses the line, complete
  or not

Shells that are missing are skipped, so CI on ubuntu still runs bash and
dash. Reading more than a shell runs (a command after ``||``, or in a line
the shell rejects) is allowed: it only makes rules stricter.
"""

from __future__ import annotations

import re

import pytest

from tstd.autonomy.classifier import DecisionClass
from tstd.policy import PolicyConfig, PolicyRule, resolve
from tstd.policy_shell import split_command
from tstd.policy_shell_scan import Scanner
from tstd.policy_shell_words import Incomplete, brace_words, peel_all, unquote
from tstd.tools import Tool

from .shell_corpus import available_shells, corpus, run_all

SHELL = Tool(name="shell")
_REDIRECT = re.compile(r"^[0-9]*(?:&>>|&>|>>|>&|>\||<>|<&|<<<|<<-|<<|>(?!\()|<(?!\())")
_MARKER = re.compile(r"m[0-9]+")
# What an expansion in an echo's arguments may print: anything but a
# marker. No atom prints its marker to stdout, so a marker in an echo's
# output is that echo's own literal text; letting `$(…)` match one would let
# a line some unseen atom wrote pass as this echo's.
_EXPANSION = r"(?:(?!m[0-9]).)*"
_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-]")
SHELLS = available_shells()

pytestmark = pytest.mark.skipif(
    "bash" not in SHELLS and "dash" not in SHELLS, reason="no POSIX shell on this host"
)

#: Forms people write every day; failing closed on them would make ``auto``
#: rules useless, so they must stay complete.
EVERYDAY = [
    "echo m1 >>o; echo m2 >>o",
    "echo m1 >>o && echo m2 >>o",
    "echo m1 >>o \\\n  --flag \\\n  m2",
    "echo m1 >>o && \\\n  echo m2 >>o",
    "echo m1 >>o | \\\n  echo m2 >>o",
    "echo m1 >>o # done",
    ": <<'EOF'\nnotes; more\nEOF\necho m1 >>o",
    ": <<EOF\nhello\nEOF\necho m1 >>o",
    "( echo m1 >>o )",
    'echo "$(echo m1 >>o)"',
]


def _expected(text: str) -> list[re.Pattern[str]]:
    """What each ``echo`` the splitter found would write to ``o``, as one
    pattern per line of output."""
    scanner = Scanner(text, "posix", 0)
    scanner.run()
    patterns: list[re.Pattern[str]] = []
    for seg in scanner.segs:
        raw = seg.raw_words()
        k = peel_all([unquote(w, True) for w in raw])
        if k >= len(raw) or unquote(raw[k], True) != "echo":
            continue
        out = _Output()
        target = False
        for word in raw[k + 1 :]:
            redirect = _REDIRECT.match(word)
            if target or redirect:
                target = bool(redirect) and redirect.end() == len(word)
                continue
            out.word(word)
        patterns.extend(re.compile(line + r"\Z", re.S) for line in out.lines)
    return patterns


class _Output:
    """Regexes for the lines ``echo`` prints. An expansion may print
    anything but a marker, and an unquoted word made only of expansions
    may vanish."""

    def __init__(self) -> None:
        self.lines = [""]
        self.words = 0

    def _lit(self, text: str) -> None:
        first, *rest = text.split("\n")
        self.lines[-1] += re.escape(first)
        self.lines.extend(re.escape(line) for line in rest)

    def word(self, raw: str) -> None:
        mark = (len(self.lines), len(self.lines[-1]))
        vanish = True
        i, n = 0, len(raw)
        if brace_words(unquote(raw, True)) is not None:
            # bash's brace expansion: any words, or none (`{,}`).
            self.lines[-1] += _EXPANSION
            i = n
        while i < n:
            c = raw[i]
            if raw.startswith("\\\n", i):
                i += 2
            elif c == "\\":
                self._lit(raw[i + 1 : i + 2] or "\\")
                vanish, i = False, i + 2
            elif c == "'":
                end = raw.find("'", i + 1)
                end = n if end < 0 else end
                self._lit(raw[i + 1 : end])
                vanish, i = False, end + 1
            elif c == '"' or raw.startswith('$"', i):
                if c == "$":
                    self.lines[-1] += r"\$?"  # bash: a locale string; dash: `$` and a string
                    i += 1
                i, vanish = self._dquote(raw, i + 1), False
            elif c in "$`" or (c in "<>" and raw.startswith("(", i + 1)):
                self.lines[-1] += _EXPANSION
                i = _after_expansion(raw, i)
            else:
                self._lit(c)
                vanish, i = False, i + 1
        if self.words:
            gap = " ?" if vanish else " "
            line, col = mark
            self.lines[line - 1] = self.lines[line - 1][:col] + gap + self.lines[line - 1][col:]
        self.words += 1

    def _dquote(self, raw: str, j: int) -> int:
        while j < len(raw) and raw[j] != '"':
            if raw.startswith("\\\n", j):
                j += 2
            elif raw[j] == "\\" and raw[j + 1 : j + 2] in ('"', "\\", "$", "`"):
                self._lit(raw[j + 1])
                j += 2
            elif raw[j] in "$`":
                self.lines[-1] += _EXPANSION
                j = _after_expansion(raw, j, quoted=True)
            else:
                self._lit(raw[j])
                j += 1
        return j + 1


def _after_expansion(raw: str, i: int, *, quoted: bool = False) -> int:
    """The index after the substitution or parameter starting at *i*, found
    the way the splitter found the word around it."""
    scanner = Scanner(raw, "posix", 0)
    scanner.i = i
    try:
        if raw[i] == "`":
            scanner._backtick(in_dquote=quoted)
        elif raw[i] in "<>":
            scanner.i += 2
            scanner._sub()
        else:
            scanner._dollar(quoted=quoted)
    except Incomplete:
        return len(raw)
    if scanner.i == i + 1 and (name := _PARAMETER.match(raw, scanner.i)):
        return name.end()  # `$x`: the scanner reads the name as word text
    return scanner.i


def _never(marker: str) -> PolicyConfig:
    return PolicyConfig(
        rules=[
            PolicyRule(tool="shell", args="**", effect="auto"),
            PolicyRule(tool="shell", args=f"echo {marker} *", effect="never"),
        ]
    )


@pytest.fixture(scope="module")
def ran() -> dict[str, dict[str, list[str]]]:
    return run_all(SHELLS, list(dict.fromkeys(corpus() + EVERYDAY)))


def test_the_corpus_runs_and_mostly_splits(ran: dict[str, dict[str, list[str]]]) -> None:
    # Guards the test itself: the atoms do write, and failing closed on
    # everything would pass the checks below while helping nobody.
    assert len(ran) > 1000
    assert all(lines == ["m1", "m2"] for lines in ran["echo m1 >>o; echo m2 >>o"].values())
    complete = sum(split_command(text, ("posix",)).complete for text in ran)
    assert complete / len(ran) > 0.6


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("echo m1 $(:) >>o", "m1"),
        ('echo "$(:)" m1 >>o', " m1"),  # the quoted empty word stays
        ("echo m1 `:` $x >>o", "m1"),
    ],
)
def test_an_expansion_in_an_echo_never_stands_for_another_atom(text: str, line: str) -> None:
    # Guards the oracle (PR #20 review): `.*` for an expansion matched every
    # line, so an atom the splitter missed passed as this echo's output.
    expected = _expected(f"{text}; echo m2 >>o")
    assert any(p.match(line) for p in expected)
    assert not any(p.match("m3") for p in expected)
    assert sum(bool(p.match("m2")) for p in expected) == 1


@pytest.mark.parametrize("text", EVERYDAY)
def test_everyday_forms_stay_complete(text: str) -> None:
    assert split_command(text, ("posix",)).complete


def test_a_complete_split_reads_every_command_every_shell_runs(
    ran: dict[str, dict[str, list[str]]],
) -> None:
    failures: list[str] = []
    for text, by_shell in ran.items():
        if not split_command(text, ("posix",)).complete:
            continue
        expected = _expected(text)
        for shell, lines in by_shell.items():
            unseen = [line for line in lines if not any(p.match(line) for p in expected)]
            if unseen:
                failures.append(f"{shell}: {text!r} ran {unseen}")
    assert not failures, f"{len(failures)} disagreements:\n" + "\n".join(failures[:25])


def test_a_never_rule_sees_every_command_that_runs(
    ran: dict[str, dict[str, list[str]]],
) -> None:
    failures: list[str] = []
    for text, by_shell in ran.items():
        firsts = {line.split(" ")[0] for lines in by_shell.values() for line in lines}
        for marker in sorted(m for m in firsts if _MARKER.fullmatch(m)):
            effect = resolve(_never(marker), SHELL, {"command": text}, DecisionClass.B)
            if effect != "never":
                shells = [s for s, lines in by_shell.items() if any(marker in x for x in lines)]
                failures.append(f"{text!r}: `echo {marker}` ran under {shells}, rule said {effect}")
    assert not failures, f"{len(failures)} misses:\n" + "\n".join(failures[:25])
