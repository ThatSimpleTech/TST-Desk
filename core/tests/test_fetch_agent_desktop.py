"""Build-time fetch of the pinned agent-desktop CLI (TD-5221 Linux, TD-5228 macOS).

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
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_sidecar as sidecar
import fetch_agent_desktop as fad

from tstd.desktop.ax_cli import PINNED_RELEASE, PINNED_SHA256

REPO = Path(__file__).resolve().parents[2]
LINUX = "x86_64-unknown-linux-gnu"
MACOS = "aarch64-apple-darwin"
CLI_BYTES = b"pretend cli"
HELPER_BYTES = b"pretend helper"


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


@dataclass(frozen=True)
class Target:
    """One bundled platform: its triple, the helper its CLI starts, a good archive."""

    triple: str
    helper: str
    good: bytes


@pytest.fixture(
    params=[(LINUX, "agent-desktop-linux-clipboard-owner"), (MACOS, "agent-desktop-macos-helper")],
    ids=["linux", "macos"],
)
def target(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Target:
    """Pin a fake archive for one platform in place of the real release."""
    triple, helper = request.param
    good = _archive({fad.CLI: CLI_BYTES, helper: HELPER_BYTES})
    monkeypatch.setattr(fad, "ARCHIVE_SHA256", {triple: _sha(good)})
    monkeypatch.setattr(fad, "HELPER_SHA256", {triple: _sha(HELPER_BYTES)})
    monkeypatch.setattr(fad, "PINNED_SHA256", {triple: _sha(CLI_BYTES)})
    return Target(triple, helper, good)


class FakeDownload:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.data


class TestEnsure:
    def test_writes_both_sidecars_from_the_pinned_url(self, target: Target, tmp_path: Path) -> None:
        fetch = FakeDownload(target.good)
        paths = fad.ensure(target.triple, binaries=tmp_path, fetch=fetch)
        assert fetch.urls == [
            "https://github.com/vercel-labs/agent-desktop/releases/download/"
            f"v{PINNED_RELEASE}/agent-desktop-v{PINNED_RELEASE}-{target.triple}.tar.gz"
        ]
        assert paths == [
            tmp_path / f"agent-desktop-{target.triple}",
            tmp_path / f"{target.helper}-{target.triple}",
        ]
        assert paths[0].read_bytes() == CLI_BYTES
        assert paths[1].read_bytes() == HELPER_BYTES
        if sys.platform != "win32":
            assert all(os.access(p, os.X_OK) for p in paths)
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths)

    def test_archive_mismatch_writes_nothing(self, target: Target, tmp_path: Path) -> None:
        tampered = _archive({fad.CLI: CLI_BYTES + b"!", target.helper: HELPER_BYTES})
        with pytest.raises(fad.FetchError, match="archive"):
            fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(tampered))
        assert list(tmp_path.iterdir()) == []

    def test_cli_mismatch_inside_a_pinned_archive_writes_nothing(
        self, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The archive pin alone is not enough: the CLI must match the daemon's pin."""
        swapped = _archive({fad.CLI: b"other cli", target.helper: HELPER_BYTES})
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {target.triple: _sha(swapped)})
        with pytest.raises(fad.FetchError, match="agent-desktop for"):
            fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(swapped))
        assert list(tmp_path.iterdir()) == []

    def test_helper_mismatch_inside_a_pinned_archive_writes_nothing(
        self, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The helper has its own pin, checked even when the archive's matches."""
        swapped = _archive({fad.CLI: CLI_BYTES, target.helper: b"other helper"})
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {target.triple: _sha(swapped)})
        with pytest.raises(fad.FetchError, match=f"{target.helper} for"):
            fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(swapped))
        assert list(tmp_path.iterdir()) == []

    def test_missing_helper_fails(
        self, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lone = _archive({fad.CLI: CLI_BYTES})
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {target.triple: _sha(lone)})
        with pytest.raises(fad.FetchError, match="not in the"):
            fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(lone))
        assert list(tmp_path.iterdir()) == []

    def test_link_member_is_refused(
        self, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        linked = _archive({fad.CLI: CLI_BYTES}, link=target.helper)
        monkeypatch.setattr(fad, "ARCHIVE_SHA256", {target.triple: _sha(linked)})
        with pytest.raises(fad.FetchError, match="not a regular file"):
            fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(linked))
        assert list(tmp_path.iterdir()) == []

    def test_pinned_files_in_place_need_no_download(self, target: Target, tmp_path: Path) -> None:
        fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(target.good))
        again = FakeDownload(b"never read")
        fad.ensure(target.triple, binaries=tmp_path, fetch=again)
        assert again.urls == []

    def test_tampered_file_in_place_is_replaced(self, target: Target, tmp_path: Path) -> None:
        paths = fad.ensure(target.triple, binaries=tmp_path, fetch=FakeDownload(target.good))
        paths[0].write_bytes(b"tampered")
        fetch = FakeDownload(target.good)
        fad.ensure(target.triple, binaries=tmp_path, fetch=fetch)
        assert len(fetch.urls) == 1
        assert paths[0].read_bytes() == CLI_BYTES

    def test_local_archive_is_checked_the_same_way(self, target: Target, tmp_path: Path) -> None:
        local = tmp_path / "release.tar.gz"
        local.write_bytes(target.good)
        out = tmp_path / "bin"
        fetch = FakeDownload(b"never read")
        fad.ensure(target.triple, binaries=out, archive=local, fetch=fetch)
        assert fetch.urls == []
        assert (out / f"agent-desktop-{target.triple}").read_bytes() == CLI_BYTES
        local.write_bytes(target.good + b"x")
        (out / f"agent-desktop-{target.triple}").unlink()
        with pytest.raises(fad.FetchError):
            fad.ensure(target.triple, binaries=out, archive=local)


def test_unbundled_target_is_refused_before_any_download(tmp_path: Path) -> None:
    fetch = FakeDownload(b"never read")
    with pytest.raises(fad.FetchError, match="no pinned"):
        fad.ensure("x86_64-pc-windows-msvc", binaries=tmp_path, fetch=fetch)
    assert fetch.urls == []
    assert list(tmp_path.iterdir()) == []


class TestRealPins:
    def test_linux_and_macos_targets(self) -> None:
        assert set(fad.ARCHIVE_SHA256) == {
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        }
        assert set(fad.HELPER_SHA256) == set(fad.ARCHIVE_SHA256)

    def test_cli_pin_is_the_daemons_pin(self) -> None:
        for triple in fad.ARCHIVE_SHA256:
            assert fad.expected(triple)[fad.CLI] == PINNED_SHA256[triple]

    def test_each_platform_reads_its_own_helper(self) -> None:
        for triple in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
            assert set(fad.expected(triple)) == {"agent-desktop", "agent-desktop-macos-helper"}
        for triple in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"):
            assert set(fad.expected(triple)) == {
                "agent-desktop",
                "agent-desktop-linux-clipboard-owner",
            }

    def test_packaged_names_are_the_ones_the_cli_looks_for(self) -> None:
        """Tauri drops ``-<triple>``; the macOS CLI accepts its helper only by exact name."""
        for triple in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
            packaged = {
                path.name.replace(f"-{triple}", "")
                for path in fad.sidecar_paths(triple, Path("binaries")).values()
            }
            assert packaged == {"agent-desktop", "agent-desktop-macos-helper"}

    def test_digests_are_sha256_hex(self) -> None:
        for table in (fad.ARCHIVE_SHA256, fad.HELPER_SHA256):
            for digest in table.values():
                assert len(digest) == 64 and int(digest, 16) >= 0
        assert len({*fad.ARCHIVE_SHA256.values(), *fad.HELPER_SHA256.values()}) == 8


PLATFORM_CONFIGS = [
    ("tauri.linux.conf.json", fad.LINUX_HELPER, fad.MACOS_HELPER),
    ("tauri.macos.conf.json", fad.MACOS_HELPER, fad.LINUX_HELPER),
]


@pytest.mark.parametrize(
    ("name", "helper", "other_helper"), PLATFORM_CONFIGS, ids=["linux", "macos"]
)
class TestTauriPlatformConfig:
    """A platform config replaces externalBin wholesale (JSON merge patch)."""

    def _load(self, name: str) -> dict[str, object]:
        data: dict[str, object] = json.loads((REPO / "shell" / name).read_text(encoding="utf-8"))
        return data

    def test_keeps_every_shared_sidecar_and_adds_both_files(
        self, name: str, helper: str, other_helper: str
    ) -> None:
        shared = self._load("tauri.conf.json")["bundle"]
        platform = self._load(name)["bundle"]
        assert isinstance(shared, dict) and isinstance(platform, dict)
        assert set(shared["externalBin"]) <= set(platform["externalBin"])
        assert {f"binaries/{fad.CLI}", f"binaries/{helper}"} <= set(platform["externalBin"])
        # The other platform's helper is never fetched here, so listing it fails the build.
        assert f"binaries/{other_helper}" not in platform["externalBin"]

    def test_bundles_licence_and_notice(self, name: str, helper: str, other_helper: str) -> None:
        platform = self._load(name)["bundle"]
        assert isinstance(platform, dict)
        resources = platform["resources"]
        assert isinstance(resources, dict)
        for source in resources:
            assert (REPO / "shell" / source).resolve().is_file(), source
        assert {Path(s).name for s in resources} == {"LICENSE", "NOTICE"}


@pytest.mark.parametrize(
    ("platform", "triple"),
    [("linux", "riscv64gc-unknown-linux-gnu"), ("darwin", "arm64e-apple-darwin")],
)
def test_build_fails_without_a_pin(
    monkeypatch: pytest.MonkeyPatch, platform: str, triple: str
) -> None:
    """build_sidecar stops before building when the host's target has no pinned CLI."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sidecar, "host_triple", lambda: triple)
    monkeypatch.setattr(sys, "argv", ["build_sidecar.py"])

    def no_build(triple: str) -> Path:
        raise AssertionError("built anyway")

    monkeypatch.setattr(sidecar, "build", no_build)
    assert sidecar.main() == 1


@pytest.mark.parametrize(
    ("platform", "triple", "fetched"),
    [("linux", LINUX, True), ("darwin", MACOS, True), ("win32", "x86_64-pc-windows-msvc", False)],
)
def test_if_missing_still_fetches_where_the_package_carries_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, triple: str, fetched: bool
) -> None:
    """``tauri dev`` (``--if-missing``) gets the CLI the same way a package build does."""
    built = tmp_path / f"tstd-{triple}"
    built.write_bytes(b"sidecar")
    calls: list[str] = []

    def ensure(triple: str) -> list[Path]:
        calls.append(triple)
        return []

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sidecar, "host_triple", lambda: triple)
    monkeypatch.setattr(sidecar, "target_path", lambda triple: built)
    monkeypatch.setattr(fad, "ensure", ensure)
    monkeypatch.setattr(sys, "argv", ["build_sidecar.py", "--if-missing"])
    assert sidecar.main() == 0
    assert calls == ([triple] if fetched else [])
