"""Build-time fetch of the pinned agent-desktop CLI for Linux packages (TD-5221).

A build gets exactly the pinned files or fails: the archive and both files
in it are checked, only those two members are read, and nothing is written
on a mismatch. No test touches the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_sidecar as sidecar
import fetch_agent_desktop as fad

from tstd.desktop.ax_cli import PINNED_RELEASE, PINNED_SHA256

REPO = Path(__file__).resolve().parents[2]
TRIPLE = "x86_64-unknown-linux-gnu"
CLI_BYTES = b"\x7fELF pretend cli"
HELPER_BYTES = b"\x7fELF pretend helper"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive(members: dict[str, bytes], *, link: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
        if link is not None:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


GOOD = _archive({fad.CLI: CLI_BYTES, fad.HELPER: HELPER_BYTES})


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the fake archive in place of the real release."""
    monkeypatch.setattr(fad, "ARCHIVE_SHA256", {TRIPLE: _sha(GOOD)})
    monkeypatch.setattr(fad, "HELPER_SHA256", {TRIPLE: _sha(HELPER_BYTES)})
    monkeypatch.setattr(fad, "PINNED_SHA256", {TRIPLE: _sha(CLI_BYTES)})


class FakeDownload:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.data


@pytest.mark.usefixtures("pins")
class TestEnsure:
    def test_writes_both_sidecars_from_the_pinned_url(self, tmp_path: Path) -> None:
        fetch = FakeDownload(GOOD)
        paths = fad.ensure(TRIPLE, binaries=tmp_path, fetch=fetch)
        assert fetch.urls == [
            "https://github.com/vercel-labs/agent-desktop/releases/download/"
            f"v{PINNED_RELEASE}/agent-desktop-v{PINNED_RELEASE}-{TRIPLE}.tar.gz"
        ]
        assert paths == [
            tmp_path / f"agent-desktop-{TRIPLE}",
            tmp_path / f"agent-desktop-linux-clipboard-owner-{TRIPLE}",
        ]
        assert paths[0].read_bytes() == CLI_BYTES
        assert paths[1].read_bytes() == HELPER_BYTES
        if sys.platform != "win32":
            assert all(os.access(p, os.X_OK) for p in paths)
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths)

    def test_archive_mismatch_writes_nothing(self, tmp_path: Path) -> None:
        tampered = _archive({fad.CLI: CLI_BYTES + b"!", fad.HELPER: HELPER_BYTES})
        with pytest.raises(fad.FetchError, match="archive"):
            fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(tampered))
        assert list(tmp_path.iterdir()) == []

    def test_cli_mismatch_inside_a_pinned_archive_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The archive pin alone is not enough: the CLI must match the daemon's pin."""
        swapped = _archive({fad.CLI: b"other cli", fad.HELPER: HELPER_BYTES})
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {TRIPLE: _sha(swapped)})
        with pytest.raises(fad.FetchError, match="agent-desktop for"):
            fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(swapped))
        assert list(tmp_path.iterdir()) == []

    def test_missing_helper_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lone = _archive({fad.CLI: CLI_BYTES})
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {TRIPLE: _sha(lone)})
        with pytest.raises(fad.FetchError, match="not in the"):
            fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(lone))
        assert list(tmp_path.iterdir()) == []

    def test_link_member_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        linked = _archive({fad.CLI: CLI_BYTES}, link=fad.HELPER)
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {TRIPLE: _sha(linked)})
        with pytest.raises(fad.FetchError, match="not a regular file"):
            fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(linked))
        assert list(tmp_path.iterdir()) == []

    def test_pinned_files_in_place_need_no_download(self, tmp_path: Path) -> None:
        fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(GOOD))
        again = FakeDownload(b"never read")
        fad.ensure(TRIPLE, binaries=tmp_path, fetch=again)
        assert again.urls == []

    def test_tampered_file_in_place_is_replaced(self, tmp_path: Path) -> None:
        paths = fad.ensure(TRIPLE, binaries=tmp_path, fetch=FakeDownload(GOOD))
        paths[0].write_bytes(b"tampered")
        fetch = FakeDownload(GOOD)
        fad.ensure(TRIPLE, binaries=tmp_path, fetch=fetch)
        assert len(fetch.urls) == 1
        assert paths[0].read_bytes() == CLI_BYTES

    def test_local_archive_is_checked_the_same_way(self, tmp_path: Path) -> None:
        local = tmp_path / "release.tar.gz"
        local.write_bytes(GOOD)
        out = tmp_path / "bin"
        fetch = FakeDownload(b"never read")
        fad.ensure(TRIPLE, binaries=out, archive=local, fetch=fetch)
        assert fetch.urls == []
        assert (out / f"agent-desktop-{TRIPLE}").read_bytes() == CLI_BYTES
        local.write_bytes(GOOD + b"x")
        (out / f"agent-desktop-{TRIPLE}").unlink()
        with pytest.raises(fad.FetchError):
            fad.ensure(TRIPLE, binaries=out, archive=local)

    def test_unbundled_target_is_refused_before_any_download(self, tmp_path: Path) -> None:
        fetch = FakeDownload(GOOD)
        with pytest.raises(fad.FetchError, match="no pinned"):
            fad.ensure("aarch64-apple-darwin", binaries=tmp_path, fetch=fetch)
        assert fetch.urls == []


class TestRealPins:
    def test_linux_targets_only(self) -> None:
        assert set(fad.ARCHIVE_SHA256) == {
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
        }
        assert set(fad.HELPER_SHA256) == set(fad.ARCHIVE_SHA256)

    def test_cli_pin_is_the_daemons_pin(self) -> None:
        for triple in fad.ARCHIVE_SHA256:
            assert fad.expected(triple)[fad.CLI] == PINNED_SHA256[triple]

    def test_digests_are_sha256_hex(self) -> None:
        for table in (fad.ARCHIVE_SHA256, fad.HELPER_SHA256):
            for digest in table.values():
                assert len(digest) == 64 and int(digest, 16) >= 0


class TestTauriLinuxConfig:
    """tauri.linux.conf.json replaces externalBin wholesale (JSON merge patch)."""

    def _load(self, name: str) -> dict[str, object]:
        data: dict[str, object] = json.loads((REPO / "shell" / name).read_text(encoding="utf-8"))
        return data

    def test_keeps_every_shared_sidecar_and_adds_both_files(self) -> None:
        shared = self._load("tauri.conf.json")["bundle"]
        linux = self._load("tauri.linux.conf.json")["bundle"]
        assert isinstance(shared, dict) and isinstance(linux, dict)
        assert set(shared["externalBin"]) <= set(linux["externalBin"])
        assert {f"binaries/{fad.CLI}", f"binaries/{fad.HELPER}"} <= set(linux["externalBin"])

    def test_bundles_licence_and_notice(self) -> None:
        linux = self._load("tauri.linux.conf.json")["bundle"]
        assert isinstance(linux, dict)
        resources = linux["resources"]
        assert isinstance(resources, dict)
        for source in resources:
            assert (REPO / "shell" / source).resolve().is_file(), source
        assert {Path(s).name for s in resources} == {"LICENSE", "NOTICE"}


def test_build_fails_on_linux_without_a_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_sidecar stops before building when the Linux target has no pinned CLI."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sidecar, "host_triple", lambda: "riscv64gc-unknown-linux-gnu")
    monkeypatch.setattr(sys, "argv", ["build_sidecar.py"])

    def no_build(triple: str) -> Path:
        raise AssertionError("built anyway")

    monkeypatch.setattr(sidecar, "build", no_build)
    assert sidecar.main() == 1
