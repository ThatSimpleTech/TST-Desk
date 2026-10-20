"""The read-only list's reader against real shells (TD-5103).

``segments`` claims that, for any line it accepts, the words of each
command are exactly what the program receives, and that nothing else
runs. This test holds it to that: a seeded corpus of lines built from two
stub programs, plain and quoted words and every separator the reader
takes, run under bash, ``bash --posix``, dash, ``sh`` and busybox ash
(a missing one is skipped). Each stub logs its argv. Every logged argv
must be one of the reader's commands, and with every stub exiting 0 and
no ``||`` in the line, every command must have run.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tstd.shell_read_only_args import segments

from .shell_corpus import available_shells

SHELLS = available_shells()
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not SHELLS, reason="no POSIX shell on this host"
)

_UNIT = "\x1f"
# One file per run of a stub, named by its pid: the commands of a pipeline
# run at once, and one shared log would interleave their writes.
_STUB = (
    "#!/bin/sh\n"
    '{ printf \'%s\' "${0##*/}"; for a in "$@"; do printf \'\\037%s\' "$a"; done; }'
    ' >"$LOG/$$"\n'
)
WORDS = [
    "a",
    "-x",
    "-la",
    "--y=z",
    "a.b/c",
    "x,y",
    "+1",
    "@u",
    "%p",
    "HEAD~1",
    "x^",
    "a=b",
    "''",
    '""',
    "'a b'",
    '"c d"',
    "'$x'",
    "'`x`'",
    '"e\'f"',
    "'g\"h'",
    "'#'",
    '"#"',
    "'*'",
    "'~'",
    "a'b'c",
    "\"x\"y'z'",
    "'a;b'",
    '"a|b"',
    "'&&'",
    "';'",
    "'\\'",
    '"{a,b}"',
    "'>x'",
    "x'~'",
]
SEPARATORS = [" ; ", ";", " && ", "&&", " | ", "|", "\n", " ;\n", "\t|\t", " || ", "\n\n"]
FIXED = [
    "p a",
    "p 'a b' \"c d\" e",
    "p a;q b",
    "p a&&q b",
    "p a|q b",
    "p a ;\n",
    "  p\ta  ",
    "p a'b'\"c\"",
    "p ''",
    "p x=~",
    "p a\\b",
    "p a #b",
    "p a&q",
]


def _corpus() -> list[str]:
    rng = random.Random(5103)
    lines = list(FIXED)
    for _ in range(400):
        parts: list[str] = []
        for k in range(rng.randint(1, 3)):
            if k:
                parts.append(rng.choice(SEPARATORS))
            words = [rng.choice(("p", "q")), *rng.choices(WORDS, k=rng.randint(0, 4))]
            parts.append(" ".join(words))
        lines.append("".join(parts))
    return lines


def _ran(index: int, argv: tuple[str, ...], line: str, tmp: Path) -> list[tuple[str, ...]]:
    log = tmp / f"log-{index}"
    log.mkdir()
    env = {"PATH": str(tmp / "bin"), "LOG": str(log)}
    shell = shutil.which(argv[0])
    assert shell is not None
    subprocess.run(
        [shell, *argv[1:], line], env=env, cwd=tmp, capture_output=True, timeout=20, check=False
    )
    return [tuple(run.read_text().split(_UNIT)) for run in log.iterdir()]


def test_the_reader_matches_what_the_shells_run(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    for name in ("p", "q"):
        stub = tmp_path / "bin" / name
        stub.write_text(_STUB)
        stub.chmod(0o755)
    accepted = [(line, segs) for line in _corpus() if (segs := segments(line)) is not None]
    assert len(accepted) > 200, "the corpus should mostly be lines the reader takes"
    rejected = [line for line in FIXED if segments(line) is None]
    assert rejected == ["p x=~", "p a\\b", "p a #b", "p a&q"]

    jobs = [(line, segs, argv) for line, segs in accepted for argv in SHELLS.values()]
    with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 2) * 2)) as pool:
        runs = list(pool.map(lambda i: _ran(i, jobs[i][2], jobs[i][0], tmp_path), range(len(jobs))))
    failures: list[str] = []
    for (line, segs, argv), ran in zip(jobs, runs, strict=True):
        expected = Counter(tuple(seg) for seg in segs)
        got = Counter(ran)
        unseen = got - expected
        if unseen:
            failures.append(f"{argv[0]} ran {sorted(unseen)} for {line!r}; read {segs}")
        elif "||" not in line and got != expected:
            failures.append(f"{argv[0]} ran {sorted(got)} for {line!r}; read {segs}")
    assert not failures, "\n".join(failures[:20])
