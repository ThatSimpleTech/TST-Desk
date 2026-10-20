"""Backup endpoints per tier: the config side (TD-5120).

``fallbacks`` lives in config only. These check what loads, what is
refused, that the settings writers and key deletion leave a backup's
lines intact, and the one line ``tst run`` prints when a turn moves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.test_backup_endpoints import BACKUP, PRIMARY, SECOND, _tier
from tstd.cli_turn import _TurnReader
from tstd.config import ConfigError, load_config
from tstd.config_write import save_tier_credential, save_tier_slug
from tstd.daemon import Daemon
from tstd.protocol import ModelFallback, parse_daemon_event


def _yaml_config(tmp_path: Path, brain: dict[str, Any], **extra: Any) -> Path:
    tier = {
        "slug": "m",
        "base_url": "http://127.0.0.1:8080/v1",
        "input_price": 0,
        "output_price": 0,
        "cache_read_price": 0,
        "context_window": 32768,
        "max_output_tokens": 1000,
    }
    doc = {
        "presets": {"p": {"brain": {**tier, **brain}, "worker": tier, "validator": tier}},
        "active_preset": "p",
        **extra,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


_BACKUP_YAML = {
    "slug": "vendor/backup-model",
    "base_url": "https://api.example.com/v1",
    "credential": "spare",
    "input_price": 3.0,
    "output_price": 15.0,
    "cache_read_price": 0.3,
    "context_window": 200000,
    "max_output_tokens": 8000,
}


class TestConfig:
    def test_a_tier_lists_ordered_backups_in_config(self, tmp_path: Path) -> None:
        second = {**_BACKUP_YAML, "slug": "vendor/second", "credential": None}
        config = load_config(
            _yaml_config(
                tmp_path,
                {"fallbacks": [_BACKUP_YAML, second]},
                credentials={"spare": {"name": "Spare"}},
            )
        )

        backups = config.tier("brain").fallbacks
        assert [b.slug for b in backups] == ["vendor/backup-model", "vendor/second"]
        assert backups[0].credential == "spare"
        assert backups[0].context_window == 200000
        assert config.tier("worker").fallbacks == []
        assert config.fallback_bindings("spare") == ["presets.p.brain.fallbacks[0]"]
        assert config.fallback_bindings("openrouter") == []

    def test_a_backup_names_its_model(self, tmp_path: Path) -> None:
        local = {k: v for k, v in _BACKUP_YAML.items() if k not in ("slug", "credential")}
        local["base_url"] = "http://127.0.0.1:9090/v1"
        with pytest.raises(ConfigError, match=r"fallbacks\[0\] must name its model"):
            load_config(_yaml_config(tmp_path, {"fallbacks": [local]}))

    def test_backups_do_not_nest(self) -> None:
        inner = _tier("inner", SECOND)
        with pytest.raises(ValidationError, match="cannot list fallbacks of its own"):
            _tier("own", PRIMARY, fallbacks=[_tier("outer", BACKUP, fallbacks=[inner])])

    def test_a_backup_key_must_be_declared(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"presets\.p\.brain\.fallbacks\[0\]\.credential"):
            load_config(_yaml_config(tmp_path, {"fallbacks": [_BACKUP_YAML]}))

    def test_nothing_is_inherited_from_the_tier(self, tmp_path: Path) -> None:
        partial = {"slug": "vendor/backup-model", "base_url": "https://api.example.com/v1"}
        with pytest.raises(ConfigError, match="input_price"):
            load_config(_yaml_config(tmp_path, {"fallbacks": [partial]}))


class TestSettingsWrites:
    def test_writing_a_slug_or_key_leaves_the_backups_alone(self, tmp_path: Path) -> None:
        # PyYAML puts the list's dashes at the key's own indent: the same
        # indent as the tier's own keys, which is what the writers look for.
        path = _yaml_config(
            tmp_path,
            {"fallbacks": [_BACKUP_YAML]},
            credentials={"spare": {"name": "Spare"}},
        )
        save_tier_slug("p", "brain", "new-model", path)
        save_tier_credential("p", "brain", "spare", path)
        save_tier_credential("p", "brain", None, path)

        brain = load_config(path).tier("brain")
        assert (brain.slug, brain.credential) == ("new-model", None)
        (backup,) = brain.fallbacks
        assert (backup.slug, backup.credential) == ("vendor/backup-model", "spare")


class TestTstRun:
    async def test_text_output_notes_the_switch_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        moved = ModelFallback(
            session_id="s",
            tier="brain",
            from_model="own-model",
            from_host="primary.local",
            to_model="backup-model",
            to_host="backup.local",
            fallback=1,
            reason="connection_error",
            seq=2,
        )
        reader = _TurnReader(session_id="s", output="text", ours=True, content=None)
        assert await reader.feed(None, json.loads(moved.model_dump_json())) is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == (
            "(brain endpoint failed: connection_error; "
            "asking backup 1, backup-model on backup.local)"
        )
        assert parse_daemon_event(moved.model_dump_json()) == moved


class TestDeletingABackupsKey:
    async def test_is_refused_while_a_backup_names_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = load_config(
            _yaml_config(
                tmp_path,
                {"fallbacks": [_BACKUP_YAML]},
                credentials={"spare": {"name": "Spare"}},
            )
        )
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: config)
        deleted: list[str] = []

        async def _delete(provider_name: str = "openrouter") -> None:
            deleted.append(provider_name)

        monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
        daemon = Daemon(data_dir=tmp_path)
        raw = await daemon._handle_message(
            json.dumps({"type": "delete_credential", "credential": "spare"}), None
        )

        assert raw is not None
        reply = json.loads(raw)
        assert (reply["type"], reply["code"]) == ("error", "bad_request")
        assert "presets.p.brain.fallbacks[0]" in reply["message"]
        # Nothing was deleted: the key and the config that names it stay.
        assert deleted == []
        assert "spare" in daemon.config.credentials
