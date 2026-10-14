"""The workspace map cannot push the brain prompt past the model (TD-5006).

A phone session on the home folder sent a 1,515,768-character system
prompt: the non-git walk listed 20,020 files, 19,879 of them under
``.cache``, against caps of 20,000 entries and 5 MB.  The map now gets a
share of the brain tier's window, and the walk skips hidden directories.
"""

from __future__ import annotations

from pathlib import Path

from tstd.context.manifest import (
    _DEFAULT_MAX_BYTES,
    MANIFEST_WINDOW_PERCENT,
    ManifestConfig,
    WorkspaceManifest,
    manifest_config_for_window,
)
from tstd.context.prompt import PromptAssembler
from tstd.context.tokens import CHARS_PER_TOKEN


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_the_budget_is_a_share_of_the_window() -> None:
    cfg = manifest_config_for_window(32_768)
    assert cfg.max_bytes == 32_768 * CHARS_PER_TOKEN * MANIFEST_WINDOW_PERCENT // 100
    assert manifest_config_for_window(1_000).max_bytes == 2_000  # floor
    assert manifest_config_for_window(50_000_000).max_bytes == _DEFAULT_MAX_BYTES  # ceiling


def test_the_walk_skips_hidden_directories(tmp_path: Path) -> None:
    for i in range(50):
        _touch(tmp_path / ".cache" / "pip" / f"blob{i}")
    _touch(tmp_path / ".config" / "app.toml")
    _touch(tmp_path / "notes.md")
    _touch(tmp_path / "dev" / "repo" / "main.py")
    _touch(tmp_path / ".bashrc")  # hidden files stay; only hidden dirs go

    text = WorkspaceManifest().build(tmp_path).text

    assert ".cache" not in text and ".config" not in text
    assert "notes.md" in text and "dev/repo/main.py" in text and ".bashrc" in text


def test_byte_truncation_ends_on_a_whole_line(tmp_path: Path) -> None:
    for i in range(400):
        _touch(tmp_path / f"file_with_a_long_name_{i:04d}.txt")

    result = WorkspaceManifest(ManifestConfig(max_bytes=3_000)).build(tmp_path)

    body, marker = result.text.rsplit("\n<!-- ", 1)
    assert result.truncated
    assert marker.startswith("... truncated at 3000 bytes")
    assert body.splitlines()[-1].endswith(".txt"), "no half-written path before the marker"
    assert len(body.encode("utf-8")) <= 3_000


def test_a_home_like_folder_fits_a_32k_brain_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for i in range(25_000):
        _touch(home / ".cache" / f"d{i % 50}" / f"f{i}")
    for i in range(3_000):
        _touch(home / "dev" / f"repo{i % 30}" / "src" / f"module_{i}.py")
    for i in range(40):
        (home / "dev" / f"repo{i % 30}-wt{i}").mkdir(parents=True, exist_ok=True)
        (home / "dev" / f"repo{i % 30}-wt{i}" / "AGENTS.md").write_text(
            "rule\n" * 300, encoding="utf-8"
        )

    window = 32_768
    assembler = PromptAssembler(
        home, home_dir=home, manifest_config=manifest_config_for_window(window)
    )
    prompt = assembler.assemble_sync("brain")

    assert ".cache" not in prompt.text
    assert len(prompt.text) / CHARS_PER_TOKEN < window * 0.5, (
        f"brain prompt is {len(prompt.text):,} chars for a {window}-token window"
    )
