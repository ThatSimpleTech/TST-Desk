"""The Windows key vocabulary: parity with macOS, the cmd alias, the fn refusal.

Pure table and parser tests — no desktop, no ctypes. This is where the port's
riskiest quiet failure lives: a combo that parses but means something else, or a
modifier that gets silently dropped, produces a keystroke nobody asked for.
"""

from __future__ import annotations

import pytest

from tst_cu_mcp.backends import darwin, windows

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B


class TestParityWithMacOS:
    def test_every_macos_key_name_exists_on_windows(self) -> None:
        # A combo written for a Mac must not fail on Windows for lack of a name.
        missing = sorted(set(darwin.KEYCODES) - set(windows.KEYCODES))
        assert missing == []

    def test_every_macos_modifier_is_mapped_or_explicitly_refused(self) -> None:
        # Nothing may fall through: each mac modifier is either usable on Windows
        # or named in UNSUPPORTED_MODS with a reason.
        unaccounted = sorted(set(darwin.MODS) - set(windows.MODS) - set(windows.UNSUPPORTED_MODS))
        assert unaccounted == []

    def test_fn_is_the_only_unsupported_modifier(self) -> None:
        assert set(windows.UNSUPPORTED_MODS) == {"fn"}

    def test_windows_adds_the_windows_key(self) -> None:
        # The mac table has no equivalent, so this is additive rather than parity.
        assert windows.MODS["win"] == VK_LWIN
        assert windows.MODS["super"] == VK_LWIN
        assert "win" not in darwin.MODS


class TestWindowsKeyPressedAlone:
    """Tapping Win opens Start, so it has to work as a key and not only a modifier.

    This was a live gap: `press_keys("win")` raised "unknown key 'win'" because
    the name existed only in MODS. On macOS pressing cmd alone does nothing, so
    the original table had no reason to cover it.
    """

    @pytest.mark.parametrize("name", ["win", "super"])
    def test_pressable_on_its_own(self, name: str) -> None:
        assert windows.parse_key_combo(name) == ((), VK_LWIN)

    @pytest.mark.parametrize("name", ["win", "super"])
    def test_still_works_as_a_modifier(self, name: str) -> None:
        # The name lives in both tables; adding it to KEYCODES must not stop it
        # holding down for a combo.
        assert windows.parse_key_combo(f"{name}+d") == ((VK_LWIN,), 0x44)

    def test_in_both_tables_with_the_same_code(self) -> None:
        assert windows.KEYCODES["win"] == windows.MODS["win"] == VK_LWIN

    def test_carries_the_extended_flag(self) -> None:
        # VK_LWIN is an extended key; without the flag it does not reach Start.
        assert windows.KEYCODES["win"] in windows.EXTENDED_KEYS

    def test_ctrl_escape_remains_the_equivalent(self) -> None:
        # The workaround used before this fix; keeping it working means a caller
        # written against either spelling behaves the same.
        assert windows.parse_key_combo("ctrl+escape") == ((VK_CONTROL,), 0x1B)


class TestCmdAlias:
    @pytest.mark.parametrize("name", ["cmd", "command"])
    def test_cmd_maps_to_control(self, name: str) -> None:
        # The decision this pins: a mac-shaped combo does the useful thing rather
        # than failing. `cmd+c` must copy.
        assert windows.MODS[name] == VK_CONTROL

    def test_cmd_c_and_ctrl_c_parse_identically(self) -> None:
        assert windows.parse_key_combo("cmd+c") == windows.parse_key_combo("ctrl+c")

    def test_cmd_is_not_the_windows_key(self) -> None:
        # The alias must not swallow the Windows key: `win+d` has to stay distinct
        # from `cmd+d`, or "show desktop" becomes Ctrl+D.
        assert windows.parse_key_combo("win+d") != windows.parse_key_combo("cmd+d")


class TestParseKeyCombo:
    def test_single_modifier(self) -> None:
        assert windows.parse_key_combo("ctrl+c") == ((VK_CONTROL,), 0x43)

    def test_modifier_order_is_preserved(self) -> None:
        # Order matters for press/release nesting, so it is part of the contract.
        assert windows.parse_key_combo("ctrl+shift+t") == ((VK_CONTROL, VK_SHIFT), 0x54)

    def test_bare_key_has_no_modifiers(self) -> None:
        assert windows.parse_key_combo("return") == ((), 0x0D)

    def test_alt_and_option_are_the_same_key(self) -> None:
        assert windows.parse_key_combo("alt+f4") == windows.parse_key_combo("option+f4")
        assert windows.parse_key_combo("alt+f4") == ((VK_MENU,), 0x73)

    def test_case_and_whitespace_are_normalised(self) -> None:
        assert windows.parse_key_combo(" CTRL + Shift + T ") == (
            (VK_CONTROL, VK_SHIFT),
            0x54,
        )

    def test_fn_is_refused_with_an_explanation(self) -> None:
        # Refusing loudly is the whole point: silently dropping fn would send a
        # bare arrow key and look like it worked.
        with pytest.raises(ValueError, match="macOS-only"):
            windows.parse_key_combo("fn+up")

    def test_fn_alone_is_refused_too(self) -> None:
        with pytest.raises(ValueError, match="macOS-only"):
            windows.parse_key_combo("fn")

    def test_unknown_modifier_lists_the_valid_ones(self) -> None:
        with pytest.raises(ValueError, match="unknown modifier"):
            windows.parse_key_combo("hyper+c")

    def test_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            windows.parse_key_combo("ctrl+nosuchkey")

    def test_empty_combo(self) -> None:
        with pytest.raises(ValueError, match="empty key combo"):
            windows.parse_key_combo("")

    def test_separators_only(self) -> None:
        with pytest.raises(ValueError, match="empty key combo"):
            windows.parse_key_combo("+++")


class TestDeleteNaming:
    def test_mac_delete_is_backspace(self) -> None:
        # macOS labels the leftward-erasing key "delete". A mac-written combo
        # meaning backspace must not erase forwards on Windows.
        assert windows.KEYCODES["delete"] == 0x08
        assert windows.KEYCODES["backspace"] == 0x08

    def test_forward_delete_is_the_pc_delete_key(self) -> None:
        assert windows.KEYCODES["forward_delete"] == 0x2E


class TestExtendedKeys:
    @pytest.mark.parametrize(
        "name",
        ["left", "up", "right", "down", "home", "end", "pageup", "pagedown", "insert"],
    )
    def test_navigation_keys_are_flagged_extended(self, name: str) -> None:
        # Without KEYEVENTF_EXTENDEDKEY these reach the numeric keypad instead of
        # the dedicated key, which is a wrong-key bug rather than a failure.
        assert windows.KEYCODES[name] in windows.EXTENDED_KEYS

    def test_forward_delete_is_extended(self) -> None:
        assert windows.KEYCODES["forward_delete"] in windows.EXTENDED_KEYS

    def test_windows_key_is_extended(self) -> None:
        assert windows.MODS["win"] in windows.EXTENDED_KEYS

    @pytest.mark.parametrize("name", ["a", "1", "return", "space", "f1"])
    def test_ordinary_keys_are_not_extended(self, name: str) -> None:
        assert windows.KEYCODES[name] not in windows.EXTENDED_KEYS


class TestTableSanity:
    def test_letters_and_digits_are_complete(self) -> None:
        for letter in "abcdefghijklmnopqrstuvwxyz":
            assert windows.KEYCODES[letter] == 0x41 + ord(letter) - ord("a")
        for digit in range(10):
            assert windows.KEYCODES[str(digit)] == 0x30 + digit

    def test_function_keys_f1_to_f12(self) -> None:
        for n in range(1, 13):
            assert windows.KEYCODES[f"f{n}"] == 0x70 + n - 1

    def test_no_virtual_key_is_zero(self) -> None:
        # VK 0 is "no key"; a zero here would send an empty keystroke.
        assert 0 not in windows.KEYCODES.values()
        assert 0 not in windows.MODS.values()
