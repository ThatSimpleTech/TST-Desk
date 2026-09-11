"""App matching and allow/deny policy. No desktop required."""

from __future__ import annotations

from pathlib import Path

import pytest

from tst_cu_mcp.apps import (
    AppAccessError,
    AppInfo,
    assert_access,
    first_match,
    is_allowed,
    is_denied,
    needle_matches,
    visible_apps,
)
from tst_cu_mcp.config import Config, load_config, save_config
from tst_cu_mcp.tree import flatten_tree, resolve_path

SAFARI = AppInfo(name="Safari", pid=101, bundle_id="com.apple.Safari")
BANK = AppInfo(name="Bank of Example", pid=202, bundle_id="com.example.bank")
NOTES = AppInfo(name="Notes", pid=303, bundle_id="com.apple.Notes")


class TestNeedle:
    def test_empty_matches_nothing(self) -> None:
        assert needle_matches("", SAFARI) is False
        assert needle_matches("  ", SAFARI) is False

    def test_pid(self) -> None:
        assert needle_matches("101", SAFARI) is True
        assert needle_matches("202", SAFARI) is False

    def test_name_and_bundle_substring(self) -> None:
        assert needle_matches("safari", SAFARI) is True
        assert needle_matches("com.apple.Safari", SAFARI) is True
        assert needle_matches("apple.safari", SAFARI) is True
        assert needle_matches("notes", SAFARI) is False

    def test_first_match_prefers_exact(self) -> None:
        safari_preview = AppInfo(
            name="Safari Technology Preview",
            pid=9,
            bundle_id="com.apple.SafariTechnologyPreview",
        )
        hit = first_match("Safari", [safari_preview, SAFARI])
        assert hit is not None
        assert hit.pid == 101


class TestAccess:
    def test_denied_wins(self) -> None:
        assert is_denied(BANK, ("bank",)) is True
        with pytest.raises(AppAccessError, match="denied-apps"):
            assert_access(BANK, denied=("Bank of Example",))

    def test_empty_allowlist_allows_all_except_denied(self) -> None:
        assert is_allowed(SAFARI, ()) is True
        assert_access(SAFARI, allowed=(), denied=("bank",))

    def test_nonempty_allowlist_is_exclusive(self) -> None:
        assert is_allowed(NOTES, ("Safari",)) is False
        with pytest.raises(AppAccessError, match="allowed-apps"):
            assert_access(NOTES, allowed=("Safari",))

    def test_visible_apps_omits_denied(self) -> None:
        shown = visible_apps([SAFARI, BANK, NOTES], denied=("bank",))
        assert [app.pid for app in shown] == [101, 303]


class TestTree:
    def test_path_ids_and_noise_skipped(self) -> None:
        root = {
            "role": "AXApplication",
            "children": [
                {
                    "role": "AXWindow",
                    "title": "Inbox",
                    "actions": ["AXPress", "AXRaise"],
                    "children": [
                        {
                            "role": "AXGroup",
                            "children": [
                                {"role": "AXButton", "title": "Send", "actions": ["AXPress"]}
                            ],
                        },
                    ],
                }
            ],
        }
        elements, truncated = flatten_tree(root)
        assert truncated is False
        ids = [el["id"] for el in elements]
        assert ids == ["0", "0.0.0"]
        assert elements[0]["title"] == "Inbox"
        assert elements[1]["title"] == "Send"
        assert elements[1]["parent"] == "0"
        assert "press" in elements[1]["actions"]
        assert resolve_path(root, "0.0.0")["title"] == "Send"

    def test_query_filters(self) -> None:
        root = {
            "role": "AXApplication",
            "children": [
                {
                    "role": "AXWindow",
                    "title": "Doc",
                    "children": [
                        {"role": "AXButton", "title": "Save"},
                        {"role": "AXButton", "title": "Cancel"},
                    ],
                }
            ],
        }
        elements, _ = flatten_tree(root, query="save")
        assert [el["title"] for el in elements] == ["Save"]

    def test_max_nodes_truncates(self) -> None:
        children = [{"role": "AXButton", "title": f"b{i}"} for i in range(10)]
        root = {
            "role": "AXApplication",
            "children": [{"role": "AXWindow", "title": "W", "children": children}],
        }
        elements, truncated = flatten_tree(root, max_nodes=3)
        assert truncated is True
        assert len(elements) == 3


class TestConfigRoundtrip:
    def test_denied_and_mode_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        save_config(
            Config(
                actuation_enabled=True,
                denied_apps=("1Password",),
                allowed_apps=(),
                mode="full_control",
                unhide_on_finish=False,
            ),
            path,
        )
        loaded = load_config(path)
        assert loaded.denied_apps == ("1Password",)
        assert loaded.mode == "full_control"
        assert loaded.unhide_on_finish is False
        assert loaded.actuation_enabled is True

    def test_bad_mode_refuses_to_start(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mode: sideways\n", encoding="utf-8")
        with pytest.raises(ValueError, match="background"):
            load_config(path)
