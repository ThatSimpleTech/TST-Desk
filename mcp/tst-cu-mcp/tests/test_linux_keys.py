"""The Linux key vocabulary: parity with macOS, the cmd alias, the fn refusal."""

from __future__ import annotations

import pytest

from tst_cu_mcp.backends import darwin, linux
from tst_cu_mcp.backends.linux_keys import XK_CONTROL_L, XK_SUPER_L, char_to_keysym


class TestParityWithMacOS:
    def test_every_macos_key_name_exists_on_linux(self) -> None:
        missing = sorted(set(darwin.KEYCODES) - set(linux.KEYCODES))
        assert missing == []

    def test_every_macos_modifier_is_mapped_or_explicitly_refused(self) -> None:
        unaccounted = sorted(set(darwin.MODS) - set(linux.MODS) - set(linux.UNSUPPORTED_MODS))
        assert unaccounted == []

    def test_fn_is_the_only_unsupported_modifier(self) -> None:
        assert set(linux.UNSUPPORTED_MODS) == {"fn"}


class TestSuperKey:
    @pytest.mark.parametrize("name", ["win", "super"])
    def test_pressable_on_its_own(self, name: str) -> None:
        assert linux.parse_key_combo(name) == ((), XK_SUPER_L)

    @pytest.mark.parametrize("name", ["win", "super"])
    def test_still_works_as_a_modifier(self, name: str) -> None:
        mods, base = linux.parse_key_combo(f"{name}+d")
        assert mods == (XK_SUPER_L,)
        assert base == linux.KEYCODES["d"]


class TestCmdAlias:
    @pytest.mark.parametrize("name", ["cmd", "command"])
    def test_cmd_maps_to_control(self, name: str) -> None:
        assert linux.MODS[name] == XK_CONTROL_L

    def test_cmd_c_and_ctrl_c_parse_identically(self) -> None:
        assert linux.parse_key_combo("cmd+c") == linux.parse_key_combo("ctrl+c")

    def test_fn_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="macOS-only"):
            linux.parse_key_combo("fn+f")


class TestParser:
    def test_empty_combo_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            linux.parse_key_combo("")

    def test_unknown_key_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            linux.parse_key_combo("ctrl+not-a-key")


class TestCharToKeysym:
    def test_latin1_is_the_codepoint(self) -> None:
        keysym, shift = char_to_keysym("a")
        assert keysym == ord("a")
        assert shift is False

    def test_uppercase_asks_for_shift(self) -> None:
        keysym, shift = char_to_keysym("A")
        assert keysym == ord("A")
        assert shift is True

    def test_newline_is_return(self) -> None:
        keysym, shift = char_to_keysym("\n")
        assert keysym == linux.KEYCODES["return"]
        assert shift is False
