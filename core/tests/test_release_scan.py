"""The release scan finds private strings in builds and the tree (TD-5163).

The real denylist is private, so every test here uses a made-up one.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import release_scan

LIST = release_scan.Denylist.parse("# comment\n\nsecret-host\n\\bpat\\b\nallow: patio-safe\n")


def test_parse_reads_deny_and_allow_lines() -> None:
    assert [p.pattern for p in LIST.deny] == ["secret-host", "\\bpat\\b"]
    assert [p.pattern for p in LIST.allow] == ["patio-safe"]


def test_hits_are_case_insensitive_and_word_bounded() -> None:
    hits = list(LIST.hits("talk to SECRET-HOST now\npatter is fine\nask Pat"))
    assert len(hits) == 2
    assert "SECRET-HOST" in hits[0]
    assert "Pat" in hits[1]


def test_allow_line_exempts_the_whole_line() -> None:
    assert list(LIST.hits("pat patio-safe")) == []
    assert len(list(LIST.hits("pat\npatio-safe"))) == 1


def test_allow_reaches_only_the_match_context_on_a_long_line() -> None:
    # A minified bundle is one line: an allowed name elsewhere on it must
    # not exempt a real hit.
    line = "pat " + "x" * 200 + " patio-safe"
    assert len(list(LIST.hits(line))) == 1


def test_hits_hide_the_match_unless_shown() -> None:
    assert list(LIST.hits("ask pat", show=False)) == ["rule 2 at offset 4"]
    found = release_scan.scan_bytes(LIST, "f", b"secret-host", show=False)
    assert found == ["f: rule 1 at offset 0"]


def test_plain_file_and_folder(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "app.js").write_text("const u='secret-host:4000'")
    (tmp_path / "dist" / "ok.js").write_text("const u='localhost'")
    found = release_scan.scan_path(LIST, tmp_path / "dist")
    assert len(found) == 1
    assert found[0].startswith(str(tmp_path / "dist" / "app.js"))


def test_zlib_stream_inside_a_binary_is_inflated() -> None:
    # A PyInstaller onefile keeps modules zlib-compressed: raw grep misses them.
    blob = b"\x7fELF" + b"\0" * 100 + zlib.compress(b"doc: secret-host") + b"\0" * 50
    assert b"secret-host" not in blob
    found = release_scan.scan_bytes(LIST, "tstd", blob)
    assert found == ["tstd:zlib#0: doc: secret-host"]


def test_chance_match_in_compressed_bytes_is_not_a_hit() -> None:
    # Compressed bytes are noise that can spell a denied word by chance, and
    # the same build compresses the same way every time (0.4.0's aarch64
    # sidecar failed twice on one). The stream's inflated text is what counts.
    payload = b" ".join(b"module_%d = value(%d)" % (i, i * 7919 % 1000) for i in range(400))
    stream = zlib.compress(payload)
    text = payload.decode("latin-1")
    word = next(
        w
        for i in range(2, len(stream) - 3)
        if re.fullmatch(rb"[a-z]{3}", stream[i : i + 3])
        and (w := stream[i : i + 3].decode("latin-1")) not in text
    )
    chance = release_scan.Denylist.parse(re.escape(word) + "\n")
    blob = b"\x7fELF" + b"\0" * 100 + stream + b"\0" * 50
    assert release_scan.scan_bytes(chance, "tstd", blob) == []
    # The same word outside the stream is still caught.
    found = release_scan.scan_bytes(chance, "tstd", blob + word.encode("latin-1"))
    assert len(found) == 1 and found[0].startswith("tstd: ")


def test_nested_zlib_stream_is_found() -> None:
    inner = zlib.compress(b"x pat x")
    outer = b"head" + zlib.compress(b"pyz" + inner + b"tail")
    found = release_scan.scan_bytes(LIST, "bin", outer)
    assert any("zlib#0:zlib#0" in f for f in found)


def test_garbage_zlib_headers_are_skipped() -> None:
    assert release_scan.scan_bytes(LIST, "bin", b"\x78\x9c\xff\xff\x00" * 40) == []


def test_apk_entries_and_names_are_scanned() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("assets/config.json", '{"host": "secret-host"}')
        zf.writestr("res/pat/icon.txt", "plain")
    found = release_scan.scan_bytes(LIST, "app.apk", buf.getvalue())
    assert 'app.apk:assets/config.json: {"host": "secret-host"}' in found
    assert any("entry name" in f and "pat" in f for f in found)


def test_git_mode_scans_tracked_files_only(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.md").write_text("by pat\n")
    (tmp_path / "untracked.md").write_text("secret-host\n")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    assert release_scan.scan_git(LIST, tmp_path) == ["tracked.md: by pat"]


def test_no_list_passes_unless_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TST_RELEASE_DENYLIST", raising=False)
    monkeypatch.delenv("TST_RELEASE_DENYLIST_TEXT", raising=False)
    monkeypatch.setattr(release_scan, "DEFAULT_LIST", tmp_path / "missing.txt")
    target = tmp_path / "f.txt"
    target.write_text("secret-host")
    assert release_scan.main([str(target)]) == 0
    assert "no denylist" in capsys.readouterr().out
    assert release_scan.main(["--require", str(target)]) == 2


def test_list_from_env_text_fails_a_hit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TST_RELEASE_DENYLIST", raising=False)
    monkeypatch.setenv("TST_RELEASE_DENYLIST_TEXT", "secret-host\n")
    target = tmp_path / "f.txt"
    target.write_text("secret-host")
    assert release_scan.main([str(target)]) == 1
    target.write_text("clean")
    assert release_scan.main([str(target)]) == 0


def test_a_private_string_in_a_path_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI logs are public: the path of a hit, or a zip entry's name, can be the string."""
    import zipfile

    monkeypatch.delenv("TST_RELEASE_DENYLIST", raising=False)
    monkeypatch.setenv("TST_RELEASE_DENYLIST_TEXT", "secret-host\n")
    named = tmp_path / "secret-host-notes.txt"
    named.write_text("secret-host")
    bundle = tmp_path / "app.apk"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("assets/secret-host.json", "{}")
    assert release_scan.main([str(named), str(bundle)]) == 1
    out = capsys.readouterr().out
    assert "secret-host" not in out
    assert "[rule 1]" in out
