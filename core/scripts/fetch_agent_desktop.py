"""Fetch the pinned agent-desktop CLI for the Linux package (TD-5221).

    uv run python scripts/fetch_agent_desktop.py [--triple T] [--archive FILE]

Downloads the ``agent-desktop`` release archive for one Linux target, checks
the archive and both files inside it against pinned SHA-256 digests, and
writes them as Tauri ``externalBin`` sidecars:

    shell/binaries/agent-desktop-<triple>
    shell/binaries/agent-desktop-linux-clipboard-owner-<triple>

The bundler drops the triple, so both land beside ``tstd`` in the package,
which is where the CLI looks for its helper and where the daemon looks for
the CLI. Any digest mismatch fails the build. Nothing here runs in the app:
the packaged app never downloads.

Files already in place with the pinned digests are kept, so a rebuild needs
no network. ``--archive`` uses a local copy of the archive instead of
downloading it; it is checked the same way.

Only Linux is bundled for now. macOS needs the helper signed with the app
and its Accessibility grant (TD-4823); Windows has no live test yet.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from tstd.desktop.ax_cli import PINNED_RELEASE, PINNED_SHA256

CORE = Path(__file__).resolve().parent.parent
BINARIES = CORE.parent / "shell" / "binaries"

CLI = "agent-desktop"
HELPER = "agent-desktop-linux-clipboard-owner"

URL = (
    "https://github.com/vercel-labs/agent-desktop/releases/download/"
    "v{release}/agent-desktop-v{release}-{triple}.tar.gz"
)

#: SHA-256 of each release archive, copied from the release's checksums.txt.
ARCHIVE_SHA256: Mapping[str, str] = {
    "x86_64-unknown-linux-gnu": "ad661b7a3fd8f990a769243f4889a240ca0e2c8dab3ef5bddd6019086c71e0b4",
    "aarch64-unknown-linux-gnu": "864cb782e0afc8bf0df19622016b24bdbd1a83fc7749f784ee82aa49a7f845eb",
}

#: SHA-256 of the clipboard helper inside each archive above. The CLI's own
#: digest comes from ``ax_cli.PINNED_SHA256``, the table the daemon checks.
HELPER_SHA256: Mapping[str, str] = {
    "x86_64-unknown-linux-gnu": "bf17de139902724e86b3616970b7b7f16f5f1fb4143910b532a38dd2182720fa",
    "aarch64-unknown-linux-gnu": "dcd6f2ce9a0e92e42f4d2ee9161ea1b9f2b11c0e4b1878119a19c0a2db3630a6",
}

#: The archives are about 3.4 MB; anything far larger is not the release.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 120.0

Download = Callable[[str], bytes]


class FetchError(RuntimeError):
    """The archive or a file in it is missing or does not match its pin."""


def bundled(triple: str) -> bool:
    """True for the targets whose package carries the CLI."""
    return triple in ARCHIVE_SHA256


def expected(triple: str) -> dict[str, str]:
    """File name inside the archive → its pinned SHA-256."""
    if not bundled(triple):
        raise FetchError(f"no pinned agent-desktop {PINNED_RELEASE} archive for {triple}")
    return {CLI: PINNED_SHA256[triple], HELPER: HELPER_SHA256[triple]}


def sidecar_paths(triple: str, binaries: Path = BINARIES) -> dict[str, Path]:
    """Where each file lands: Tauri's ``externalBin`` name-triple convention."""
    return {name: binaries / f"{name}-{triple}" for name in (CLI, HELPER)}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def up_to_date(triple: str, binaries: Path = BINARIES) -> bool:
    """Both sidecars are present, executable, and match their pins."""
    pins = expected(triple)
    for name, path in sidecar_paths(triple, binaries).items():
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        if _file_sha256(path) != pins[name]:
            return False
    return True


def download(url: str) -> bytes:
    """GET *url* (GitHub redirects to its asset host), capped in size."""
    request = urllib.request.Request(url, headers={"User-Agent": "tst-desk-build"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
        data: bytes = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise FetchError(f"{url} is larger than {MAX_ARCHIVE_BYTES} bytes")
    return data


def verify_archive(data: bytes, triple: str) -> None:
    expected(triple)
    got = _sha256(data)
    if got != ARCHIVE_SHA256[triple]:
        raise FetchError(
            f"agent-desktop {PINNED_RELEASE} archive for {triple} has SHA-256 {got}, "
            f"pinned {ARCHIVE_SHA256[triple]}"
        )


def unpack(data: bytes, triple: str) -> dict[str, bytes]:
    """The two pinned files from a verified archive, each checked again.

    Members are read by exact name and must be regular files, so a link or
    a path outside the archive root is never written anywhere.
    """
    pins = expected(triple)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name, pin in pins.items():
            try:
                member = archive.getmember(name)
            except KeyError as exc:
                raise FetchError(f"{name} is not in the {triple} archive") from exc
            if not member.isreg():
                raise FetchError(f"{name} in the {triple} archive is not a regular file")
            handle = archive.extractfile(member)
            if handle is None:
                raise FetchError(f"{name} in the {triple} archive could not be read")
            body = handle.read()
            got = _sha256(body)
            if got != pin:
                raise FetchError(f"{name} for {triple} has SHA-256 {got}, pinned {pin}")
            files[name] = body
    return files


def install(files: Mapping[str, bytes], triple: str, binaries: Path = BINARIES) -> list[Path]:
    """Write each file beside the tstd sidecar, executable, atomically."""
    binaries.mkdir(parents=True, exist_ok=True)
    written = []
    for name, target in sidecar_paths(triple, binaries).items():
        fd, tmp = tempfile.mkstemp(prefix=f".{name}-", dir=binaries)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(files[name])
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written.append(target)
    return written


def ensure(
    triple: str,
    *,
    binaries: Path = BINARIES,
    archive: Path | None = None,
    fetch: Download = download,
) -> list[Path]:
    """Make the pinned sidecars present for *triple*; returns their paths."""
    expected(triple)
    if up_to_date(triple, binaries):
        return list(sidecar_paths(triple, binaries).values())
    if archive is not None:
        data = archive.read_bytes()
    else:
        data = fetch(URL.format(release=PINNED_RELEASE, triple=triple))
    verify_archive(data, triple)
    return install(unpack(data, triple), triple, binaries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triple", help="target triple (default: this host's, from rustc)")
    parser.add_argument("--archive", type=Path, help="a local copy of the release archive")
    args = parser.parse_args(argv)
    if args.triple:
        triple = args.triple
    else:
        from build_sidecar import host_triple

        triple = host_triple()
    try:
        paths = ensure(triple, archive=args.archive)
    except (FetchError, OSError) as exc:
        print(f"agent-desktop: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(f"agent-desktop {PINNED_RELEASE}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
