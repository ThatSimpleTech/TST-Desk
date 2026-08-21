"""Machine-wide project pins (TD-2806)."""

from __future__ import annotations

from pathlib import Path

from tstd.workspace_pins import load_workspace_pins, save_workspace_pins


def test_absent_is_empty(tmp_path: Path) -> None:
    assert load_workspace_pins(tmp_path) == []


def test_roundtrip(tmp_path: Path) -> None:
    save_workspace_pins(tmp_path, ["/ws/one", "/ws/two"])
    assert load_workspace_pins(tmp_path) == ["/ws/one", "/ws/two"]


def test_junk_is_empty(tmp_path: Path) -> None:
    (tmp_path / "workspace_pins.yaml").write_text("nope\n", encoding="utf-8")
    assert load_workspace_pins(tmp_path) == []
