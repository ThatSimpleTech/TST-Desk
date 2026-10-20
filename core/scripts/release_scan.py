"""Fail a release that carries a maintainer's private strings (TD-5163).

    uv run python scripts/release_scan.py --git
    uv run python scripts/release_scan.py PATH [PATH ...]

The patterns are not in this repo: a list of names, hosts and addresses
is itself private. They come from ``--denylist FILE``, else the file named
by ``TST_RELEASE_DENYLIST``, else the text in ``TST_RELEASE_DENYLIST_TEXT``
(CI passes a repository secret there), else
``~/.config/tst-desk/release-denylist.txt``. With none of those the scan
says so and exits 0, so a fork's build is not blocked by a list it never
had; ``--require`` makes a missing list an error instead.

One Python regex per line, matched case-insensitively; ``#`` starts a
comment line. ``allow: <regex>`` exempts a hit whose line also matches.

``--git`` scans every tracked file at HEAD of this checkout. PATH
arguments are files or folders: a built sidecar, ``ui/dist``, an APK.
Zip containers (APK, JAR, AAR, wheels) are opened entry by entry, and
any zlib stream inside a file is inflated and scanned too — a PyInstaller
onefile binary keeps its Python modules (docstrings included) that way,
so a grep over the raw bytes would miss them.

Exit 0 clean, 1 hits (each printed as ``path[:entry]: match``), 2 usage.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_LIST = Path.home() / ".config" / "tst-desk" / "release-denylist.txt"

# zlib header: CMF 0x78 (deflate, 32K window) and an FLG that passes FCHECK.
_ZLIB_HEADER = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
_ZLIB_CHUNK = 1 << 16
# Inflated payloads are scanned for nested streams down to this depth.
_MAX_DEPTH = 3
# A hit prints this much context either side, never the whole line.
_CONTEXT = 30


@dataclass(frozen=True)
class Denylist:
    deny: tuple[re.Pattern[str], ...]
    allow: tuple[re.Pattern[str], ...]

    @classmethod
    def parse(cls, text: str) -> Denylist:
        deny: list[re.Pattern[str]] = []
        allow: list[re.Pattern[str]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("allow:"):
                allow.append(re.compile(line[len("allow:") :].strip(), re.IGNORECASE))
            else:
                deny.append(re.compile(line, re.IGNORECASE))
        return cls(tuple(deny), tuple(allow))

    def hits(self, text: str) -> Iterator[str]:
        """Each denied match in *text* that no allow rule exempts, with context."""
        for pattern in self.deny:
            for m in pattern.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                line = text[start : end if end != -1 else len(text)]
                if any(a.search(line) for a in self.allow):
                    continue
                lo = max(m.start() - _CONTEXT, start)
                hi = min(m.end() + _CONTEXT, len(text) if end == -1 else end)
                yield text[lo:hi].replace("\r", " ").strip()


def load_denylist(path: str | None) -> Denylist | None:
    """The list from the first source that is set, or None when there is none."""
    if path:
        return Denylist.parse(Path(path).read_text(encoding="utf-8"))
    env_file = os.environ.get("TST_RELEASE_DENYLIST")
    if env_file:
        return Denylist.parse(Path(env_file).read_text(encoding="utf-8"))
    env_text = os.environ.get("TST_RELEASE_DENYLIST_TEXT")
    if env_text and env_text.strip():
        return Denylist.parse(env_text)
    if DEFAULT_LIST.is_file():
        return Denylist.parse(DEFAULT_LIST.read_text(encoding="utf-8"))
    return None


def _inflated(data: bytes) -> Iterator[bytes]:
    """Every zlib stream found in *data*, inflated. Garbage candidates are skipped."""
    view = memoryview(data)
    pos = 0
    while True:
        m = _ZLIB_HEADER.search(data, pos)
        if m is None:
            return
        start = m.start()
        d = zlib.decompressobj()
        out: list[bytes] = []
        at = start
        try:
            while not d.eof and at < len(data):
                out.append(d.decompress(view[at : at + _ZLIB_CHUNK]))
                at += _ZLIB_CHUNK
        except zlib.error:
            pos = start + 1
            continue
        if not d.eof:
            pos = start + 1
            continue
        yield b"".join(out)
        # Resume after the stream's end, not inside it.
        pos = at - len(d.unused_data)


def scan_bytes(denylist: Denylist, label: str, data: bytes, depth: int = 0) -> list[str]:
    """Hits in *data*, its zip entries and its zlib streams, as printable lines."""
    found = [f"{label}: {hit}" for hit in denylist.hits(data.decode("latin-1"))]
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if depth < _MAX_DEPTH:
                        entry = zf.read(info)
                        found += scan_bytes(denylist, f"{label}:{info.filename}", entry, depth + 1)
                    for hit in denylist.hits(info.filename):
                        found.append(f"{label}: entry name {hit}")
        except zipfile.BadZipFile:
            pass
    elif depth < _MAX_DEPTH:
        for n, blob in enumerate(_inflated(data)):
            found += scan_bytes(denylist, f"{label}:zlib#{n}", blob, depth + 1)
    return found


def scan_path(denylist: Denylist, path: Path) -> list[str]:
    """Hits in one file, or in every file under a folder."""
    if path.is_dir():
        found: list[str] = []
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            found += scan_bytes(denylist, str(child), child.read_bytes())
        return found
    return scan_bytes(denylist, str(path), path.read_bytes())


def tracked_files(root: Path) -> list[Path]:
    """Files git tracks at *root*, skipping ones deleted in the working tree."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    names = [n for n in out.decode("utf-8").split("\0") if n]
    return [root / n for n in names if (root / n).is_file()]


def scan_git(denylist: Denylist, root: Path) -> list[str]:
    """Hits in tracked files, by repo-relative path. Text only: no inflation."""
    found: list[str] = []
    for path in tracked_files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_bytes().decode("latin-1")
        found += [f"{rel}: {hit}" for hit in denylist.hits(rel)]
        found += [f"{rel}: {hit}" for hit in denylist.hits(text)]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--git", action="store_true", help="scan tracked files at HEAD")
    parser.add_argument("--denylist", help="pattern file (default: see module doc)")
    parser.add_argument("--require", action="store_true", help="fail when no list is set")
    args = parser.parse_args(argv)
    if not args.git and not args.paths:
        parser.print_usage(sys.stderr)
        return 2
    denylist = load_denylist(args.denylist)
    if denylist is None or not denylist.deny:
        print("release scan: no denylist set, nothing checked")
        return 2 if args.require else 0
    found: list[str] = []
    if args.git:
        found += scan_git(denylist, REPO)
    for path in args.paths:
        if not path.exists():
            print(f"release scan: {path} does not exist", file=sys.stderr)
            return 2
        found += scan_path(denylist, path)
    for line in found:
        print(line)
    print(f"release scan: {len(found)} hit(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
