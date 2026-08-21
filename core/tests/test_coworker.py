"""Coworker-mode persistence (TD-2902). Default on; Settings toggle is TD-2905."""

from __future__ import annotations

from pathlib import Path

from tstd.coworker import (
    coworker_path,
    ensure_coworker,
    load_coworker,
    save_coworker,
)


class TestCoworkerPersist:
    def test_absent_is_on(self, tmp_path: Path) -> None:
        assert load_coworker(tmp_path) is True

    def test_round_trip(self, tmp_path: Path) -> None:
        save_coworker(tmp_path, False)
        assert load_coworker(tmp_path) is False
        save_coworker(tmp_path, True)
        assert load_coworker(tmp_path) is True

    def test_lands_in_user_data_not_workspace_config(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        workspace_config = workspace / ".tst" / "config.yaml"
        workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
        before = workspace_config.read_text(encoding="utf-8")

        save_coworker(data, True)

        assert coworker_path(data).exists()
        assert not (workspace / "coworker.yaml").exists()
        assert workspace_config.read_text(encoding="utf-8") == before

    def test_unreadable_or_junk_is_on(self, tmp_path: Path) -> None:
        path = coworker_path(tmp_path)
        path.write_text(":::: not yaml", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("- just a list\n", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("enabled: false\n", encoding="utf-8")
        assert load_coworker(tmp_path) is False
        path.write_text('enabled: "false"\n', encoding="utf-8")
        assert load_coworker(tmp_path) is False
        path.write_text("other: true\n", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("", encoding="utf-8")
        assert load_coworker(tmp_path) is True

    def test_ensure_writes_default_on_once(self, tmp_path: Path) -> None:
        assert not coworker_path(tmp_path).exists()
        assert ensure_coworker(tmp_path) is True
        assert coworker_path(tmp_path).read_text(encoding="utf-8").find("enabled: true") >= 0
        save_coworker(tmp_path, False)
        assert ensure_coworker(tmp_path) is False
