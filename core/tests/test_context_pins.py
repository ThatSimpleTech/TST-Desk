"""Context pins (TD-2804)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.context_pins import PinOutsideError, add_pin, list_pin_cards, remove_pin
from tstd.tools.registry import create_registry


def test_add_lists_name_kind_lines(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    target = ws / "notes.md"
    target.parent.mkdir()
    target.write_text("a\nb\nc\n", encoding="utf-8")
    add_pin(ws, str(target))
    cards = list_pin_cards(ws)
    assert [(c.name, c.kind, c.lines) for c in cards] == [("notes.md", "file", 3)]
    assert (ws / ".tst" / "context" / "pins.yaml").is_file()


def test_outside_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outsider = tmp_path / "other" / "x.md"
    outsider.parent.mkdir()
    outsider.write_text("nope\n", encoding="utf-8")
    with pytest.raises(PinOutsideError):
        add_pin(ws, str(outsider))
    assert list_pin_cards(ws) == []


def test_unpin_keeps_the_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    target = ws / "keep.md"
    target.parent.mkdir()
    target.write_text("stay\n", encoding="utf-8")
    add_pin(ws, str(target))
    remove_pin(ws, "keep.md")
    assert list_pin_cards(ws) == []
    assert target.read_text(encoding="utf-8") == "stay\n"


def test_not_a_tool() -> None:
    names = {tool.name for tool in create_registry().list_tools()}
    assert "add_pin" not in names
    assert "remove_pin" not in names
    assert "list_pins" not in names
