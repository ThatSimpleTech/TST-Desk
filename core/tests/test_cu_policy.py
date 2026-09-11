"""Computer-use policy file (TD-4830). No desktop required."""

from __future__ import annotations

from pathlib import Path

from tstd.cu_policy import CuPolicy, load_cu_policy, save_cu_policy


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_cu_policy(
        CuPolicy(
            enabled=False,
            mode="full_control",
            unhide_on_finish=False,
            denied_apps=("1Password", "Bank"),
        ),
        path,
    )
    loaded = load_cu_policy(path)
    assert loaded.enabled is False
    assert loaded.mode == "full_control"
    assert loaded.unhide_on_finish is False
    assert loaded.denied_apps == ("1Password", "Bank")


def test_absent_file_is_permissive(tmp_path: Path) -> None:
    loaded = load_cu_policy(tmp_path / "missing.yaml")
    assert loaded.enabled is True
    assert loaded.mode == "background"
    assert loaded.denied_apps == ()


def test_save_preserves_overlay_flag(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("overlay:\n  enabled: false\nactuation:\n  enabled: true\n", encoding="utf-8")
    save_cu_policy(CuPolicy(denied_apps=("Notes",)), path)
    text = path.read_text(encoding="utf-8")
    assert "Notes" in text
    reloaded = load_cu_policy(path)
    assert reloaded.denied_apps == ("Notes",)
    # Overlay is not a policy field; the file still carries the previous bit.
    assert "enabled: false" in text
