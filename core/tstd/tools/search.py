"""Content search and path globs for the read tools (TD-5102).

``fs_grep`` searches file contents with a regular expression so a search
never needs a shell call, which asks.  It is a read tool: Class A inside
the workspace wall, refused outside it by the dispatcher's path guard.

Files come from ``git ls-files`` when the search root is in a git work
tree, so ``.gitignore`` is honoured; otherwise from a walk that skips the
same folders ``fs_list`` skips.  The search runs in this process, so it
behaves the same on every machine whether or not ripgrep is installed.

Matching uses the ``regex`` package, not ``re``: the stdlib engine has no
time limit and holds the GIL while it backtracks, so one model-written
pattern with nested repeats could spin for hours on an ordinary line and
freeze the whole daemon.  Here each line's match runs outside the GIL
with a time limit, the search as a whole has one, and the handler stops
it when the user stops the turn.

The glob dialect is shared with ``fs_list``: ``*`` and ``?`` stay inside
one path segment, ``**`` crosses segments, ``**/`` also matches no
folder at all, and ``{a,b}`` picks alternatives.  A glob with no ``/``
matches the file name at any depth; one with a ``/`` matches the path
from the search root.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import regex

from ..context.manifest import _FALLBACK_IGNORE
from ..git_safe import git_command

#: Output modes, named for what each line is.
OUTPUT_MODES = ("files", "content", "count")

#: Default and hard caps on result lines (files, matching lines or counts).
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_CAP = 2000

#: Context lines before and after a match are capped so one call cannot
#: return a whole file as "context".
MAX_CONTEXT_LINES = 10

#: Files bigger than this are skipped: they are logs, data or bundles,
#: and fs_read pages through them instead.
MAX_FILE_BYTES = 2 * 1024 * 1024

#: Files scanned per call.  A search that reaches it says so.
MAX_FILES_SCANNED = 20_000

#: A matching line longer than this is cut, so minified files stay readable.
MAX_LINE_CHARS = 300

#: Longest one line may take to match.  A pattern that backtracks badly
#: on a line stops the search there, with what it found so far.
LINE_MATCH_SECONDS = 1.0

#: Longest one search may run before it stops with what it found.
MAX_SEARCH_SECONDS = 30.0

_BINARY_SNIFF_BYTES = 8192


# ── globs ────────────────────────────────────────────────────────────────


def _split_braces(body: str) -> list[str]:
    """Split ``a,b,{c,d}`` on top-level commas only."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return parts


def _glob_body(pattern: str) -> str:
    """Translate one glob into a regex body (no anchors)."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    out.append("(?:.*/)?")  # zero or more folders
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1 : i + 2] in ("!", "^") else i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                inner = pattern[i + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = end
        elif ch == "{":
            depth = 0
            end = -1
            for j in range(i, n):
                if pattern[j] == "{":
                    depth += 1
                elif pattern[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end == -1:
                out.append(re.escape(ch))
            else:
                alts = _split_braces(pattern[i + 1 : end])
                out.append("(?:" + "|".join(_glob_body(a) for a in alts) + ")")
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def glob_matcher(pattern: str) -> Callable[[str], bool]:
    """A predicate over POSIX paths relative to the search root.

    A pattern without ``/`` is tested against the file name, so ``*.py``
    finds Python files at any depth.  A pattern with ``/`` is tested
    against the whole relative path; a leading ``./`` or ``/`` is dropped.
    """
    pat = pattern.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    if not pat:
        pat = "*"
    compiled = re.compile(r"\A" + _glob_body(pat) + r"\Z", re.DOTALL)
    if "/" in pat:
        return lambda rel: compiled.match(rel) is not None
    return lambda rel: compiled.match(rel.rsplit("/", 1)[-1]) is not None


def glob_crosses_folders(pattern: str) -> bool:
    """True when the glob names a path rather than only a file name."""
    return "/" in pattern.strip().lstrip("./") or "**" in pattern


# ── file enumeration ─────────────────────────────────────────────────────


def _ignored(rel: str) -> bool:
    return any(part in _FALLBACK_IGNORE for part in rel.split("/")[:-1])


# fs_grep is Class A with no approval (TD-5141), so a planted .git/config
# or a committed bare repo folder must not turn a search into a command
# nobody approved. git_command pins core.fsmonitor off and bare repos to
# explicit only (TD-5108); an embedded bare repo makes git fail instead,
# and the search falls back to the walk.
def _git_files(root: Path) -> list[str] | None:
    """Tracked plus untracked, non-ignored files under *root*, or None."""
    try:
        result = subprocess.run(
            git_command(
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ),
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return sorted({n for n in names if n})


def _walk_files(root: Path) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _FALLBACK_IGNORE)
        base = Path(dirpath)
        for name in sorted(filenames):
            yield (base / name).relative_to(root).as_posix()


def iter_files(root: Path) -> Iterator[str]:
    """Relative POSIX paths of the files under *root*, gitignore honoured."""
    listed = _git_files(root)
    if listed is None:
        yield from _walk_files(root)
        return
    for rel in listed:
        if not _ignored(rel):
            yield rel


# ── grep ─────────────────────────────────────────────────────────────────


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_text(path: Path) -> str | None:
    """The file's text, or None for binary, oversized or unreadable files."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + " …"


#: Search stopped because the user stopped the turn.
STOPPED = "Search stopped: the user stopped the turn."


def _matching_lines(
    compiled: regex.Pattern[str],
    rel: str,
    lines: list[str],
    deadline: float,
    stop: threading.Event | None,
) -> tuple[list[int], str | None]:
    """Indexes of *lines* that match, and a note when the search must end.

    The note is :data:`STOPPED` when *stop* was set.  Each match runs
    outside the GIL (``concurrent``) with a time limit, so a pattern that
    backtracks can neither freeze the daemon nor outlive Stop.
    """
    hits: list[int] = []
    for i, line in enumerate(lines):
        if stop is not None and stop.is_set():
            return hits, STOPPED
        now = time.monotonic()
        if now >= deadline:
            return hits, _over_time_note()
        try:
            found = compiled.search(
                line, concurrent=True, timeout=min(LINE_MATCH_SECONDS, deadline - now)
            )
        except TimeoutError:
            if time.monotonic() >= deadline:
                return hits, _over_time_note()
            return hits, (
                f"[stopped: the pattern took over {LINE_MATCH_SECONDS:g} s on {rel}:{i + 1}. "
                "It backtracks, most likely from nested repeats such as (a+)+ or (a|aa)+; "
                "simplify it]"
            )
        if found:
            hits.append(i)
    return hits, None


def _over_time_note() -> str:
    return f"[stopped after {MAX_SEARCH_SECONDS:g} s; narrow the pattern, path or glob]"


def grep(
    root: Path,
    pattern: str,
    *,
    glob: str = "",
    case_insensitive: bool = False,
    context: int = 0,
    output_mode: str = "files",
    max_results: int = DEFAULT_MAX_RESULTS,
    stop: threading.Event | None = None,
) -> str:
    """Search files under *root* (a directory or one file) for *pattern*.

    Setting *stop* ends the search at the next line; it then returns
    :data:`STOPPED`.
    """
    if output_mode not in OUTPUT_MODES:
        return f"Error: output_mode must be one of {', '.join(OUTPUT_MODES)}"
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE if case_insensitive else 0)
    except regex.error as e:
        return f"Error: invalid regex {pattern!r}: {e}"
    if not root.exists():
        return f"Error: path not found: {root}"

    limit = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), MAX_RESULTS_CAP))
    ctx = max(0, min(int(context or 0), MAX_CONTEXT_LINES))
    real_root = root.resolve()

    if root.is_file():
        base = root.parent
        candidates: Iterator[str] = iter([root.name])
        matches_glob: Callable[[str], bool] = lambda _rel: True  # noqa: E731
    else:
        base = root
        candidates = iter_files(root)
        matches_glob = glob_matcher(glob) if glob else (lambda _rel: True)

    lines: list[str] = []
    total = 0  # files (files/count modes) or matching lines (content mode)
    shown = 0  # how many of those made it under the cap
    scanned = 0
    skipped = 0
    hit_scan_cap = False
    halt: str | None = None
    walled = real_root if root.is_dir() else real_root.parent
    deadline = time.monotonic() + MAX_SEARCH_SECONDS

    for rel in candidates:
        if stop is not None and stop.is_set():
            return STOPPED
        if halt is None and time.monotonic() >= deadline:
            halt = _over_time_note()
        if halt is not None:
            break
        if not matches_glob(rel):
            continue
        if scanned >= MAX_FILES_SCANNED:
            hit_scan_cap = True
            break
        path = base / rel
        # A link may point out of the walled folder; the guard only judged
        # the search root, so follow nothing that leaves it.
        if not _inside(path.resolve(), walled):
            skipped += 1
            continue
        if not path.is_file():
            continue
        scanned += 1
        text = _read_text(path)
        if text is None:
            continue
        file_lines = text.splitlines()
        hits, halt = _matching_lines(compiled, rel, file_lines, deadline, stop)
        if halt == STOPPED:
            return STOPPED
        if not hits:
            continue

        if output_mode != "content":
            total += 1
            if shown < limit:
                shown += 1
                lines.append(rel if output_mode == "files" else f"{rel}:{len(hits)}")
            continue

        hit_set = set(hits)
        printed: set[int] = set()
        last = -1
        for i in hits:
            total += 1
            if shown >= limit:
                continue
            shown += 1
            lo, hi = max(0, i - ctx), min(len(file_lines) - 1, i + ctx)
            if ctx and lines and lo > last + 1:
                lines.append("--")
            for j in range(lo, hi + 1):
                if j in printed:
                    continue
                printed.add(j)
                sep = ":" if j in hit_set else "-"
                lines.append(f"{rel}{sep}{j + 1}{sep} {_clip(file_lines[j])}")
            last = hi

    out = "\n".join(lines) if lines else "(no matches)"
    notes: list[str] = []
    if total > shown:
        unit = "matching lines" if output_mode == "content" else "files"
        notes.append(
            f"[truncated: showing the first {shown} of {total} {unit}; "
            "narrow the pattern, path or glob, or raise max_results]"
        )
    if hit_scan_cap:
        notes.append(f"[stopped after scanning {MAX_FILES_SCANNED} files; narrow the path or glob]")
    if halt is not None:
        notes.append(halt)
    if skipped:
        notes.append(f"[skipped {skipped} linked file(s) that point outside {root}]")
    if notes:
        out += "\n… " + " ".join(notes)
    return out
