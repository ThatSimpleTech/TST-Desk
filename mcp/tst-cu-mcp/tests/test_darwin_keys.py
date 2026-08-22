"""Darwin typing wire format: what each keystroke declares to CoreGraphics.

The Windows side splits text into explicit UTF-16 units (``utf16_units``); the
macOS side hands whole characters to ``CGEventKeyboardSetUnicodeString``, whose
length argument counts UTF-16 code units, not Python characters. These tests run
against a stubbed Quartz, so they need neither a desktop nor macOS APIs — the
same property that lets ``test_backend_selection`` prove both platforms anywhere.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, cast

import pytest

from tst_cu_mcp.backends.darwin import DarwinBackend
from tst_cu_mcp.input_control import utf16_length


class FakeQuartz:
    """Records the unicode-string declarations ``type_text`` would post."""

    def __init__(self) -> None:
        self.kCGHIDEventTap: Any = object()
        self.declared: list[tuple[int, str]] = []

    def CGEventCreateKeyboardEvent(self, _source: Any, _keycode: int, _pressed: bool) -> object:
        return object()

    def CGEventKeyboardSetUnicodeString(self, _event: object, length: int, string: str) -> None:
        self.declared.append((length, string))

    def CGEventPost(self, _tap: Any, _event: object) -> None:
        pass


@pytest.fixture
def quartz(monkeypatch: pytest.MonkeyPatch) -> FakeQuartz:
    fake = FakeQuartz()
    monkeypatch.setitem(sys.modules, "Quartz", cast(ModuleType, fake))
    return fake


def test_utf16_length_counts_wire_units_not_characters() -> None:
    assert utf16_length("") == 0
    assert utf16_length("café") == 4
    # One character above U+FFFF is two units on the wire.
    assert utf16_length("\U0001f600") == 2


class TestTypeTextDeclaresWholeCharacters:
    def test_bmp_character_declares_one_unit_per_event(self, quartz: FakeQuartz) -> None:
        DarwinBackend().type_text("a")
        assert quartz.declared == [(1, "a"), (1, "a")]

    def test_astral_character_declares_both_surrogate_units(self, quartz: FakeQuartz) -> None:
        # Declaring len(char) == 1 here attached only the high surrogate: the
        # focus received replacement garbage instead of the emoji.
        DarwinBackend().type_text("\U0001f600")
        assert quartz.declared == [(2, "\U0001f600"), (2, "\U0001f600")]

    def test_mixed_text_keeps_one_down_up_pair_per_character(self, quartz: FakeQuartz) -> None:
        DarwinBackend().type_text("a\U0001f600b")
        assert [length for length, _ in quartz.declared] == [1, 1, 2, 2, 1, 1]
