"""Every config.yaml write loads before it lands (ONBOARD-01, SETMODEL-01, SETMODEL-02).

The writers edit the file line by line, to keep its comments. A line edit
under a YAML alias (`worker: *main`, the shape docs/models.md teaches) made a
file that no longer parsed, and an edit under an anchor quietly changed every
role that shared it. Removing a key a model used deleted the secret, cut the
catalog row and then failed to unbind it, leaving a file the daemon could not
start on. Now each write is checked as the daemon would load it, a shared role
is refused with a sentence, and a key is unbound before anything is deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tstd.card_write import save_tier_values
from tstd.config import ConfigError, load_config
from tstd.config_write import (
    SHARED_ROLE,
    save_active_preset,
    save_tier_credential,
    save_tier_slug,
    write_config,
)
from tstd.daemon import Daemon

ANCHORED = """\
credentials:
  acme:
    name: Acme
    base_url: https://models.example.com/v1
presets:
  main:
    label: Main
    brain: &main_tier
      base_url: http://127.0.0.1:8000/v1
      slug: main-model
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *main_tier
    validator: *main_tier
  team:
    label: Team
    brain: *main_tier
    worker:
      base_url: http://127.0.0.1:8000/v1
      slug: team-worker
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    validator: *main_tier
active_preset: main
"""


def _file(tmp_path: Path, text: str = ANCHORED) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    load_config(path)
    return path


class TestTheGuard:
    def test_a_write_that_would_not_load_leaves_the_file_alone(self, tmp_path: Path) -> None:
        path = _file(tmp_path)
        before = path.read_bytes()
        with pytest.raises(ConfigError, match="would no longer load"):
            write_config(path, "presets: [unclosed\n")
        with pytest.raises(ConfigError, match="would no longer load"):
            # ONBOARD-01's shape: a model naming a key the file no longer declares.
            dangling = ANCHORED.replace(
                "slug: team-worker", "slug: team-worker\n      credential: gone"
            )
            write_config(path, dangling)
        # A shape pydantic refuses (a model naming two of its three roles).
        with pytest.raises(ConfigError, match="would no longer load"):
            write_config(path, ANCHORED.replace("    validator: *main_tier\n  team:", "  team:"))
        assert path.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))

    def test_a_good_write_lands(self, tmp_path: Path) -> None:
        path = _file(tmp_path)
        save_active_preset("team", path)
        assert load_config(path).active_preset == "team"


class TestSharedRoles:
    @pytest.mark.parametrize(
        ("preset", "tier"),
        [("main", "worker"), ("main", "validator"), ("main", "brain"), ("team", "brain")],
    )
    def test_every_tier_writer_refuses_a_shared_role(
        self, tmp_path: Path, preset: str, tier: str
    ) -> None:
        path = _file(tmp_path)
        before = path.read_bytes()
        writers = [
            lambda: save_tier_slug(preset, tier, "other-model", path),
            lambda: save_tier_values(preset, tier, {"context_window": 65536}, path=path),
            lambda: save_tier_credential(preset, tier, "acme", path),
        ]
        for write in writers:
            with pytest.raises(ConfigError) as refused:
                write()
            assert str(refused.value) == SHARED_ROLE
            assert path.read_bytes() == before

    def test_a_role_of_its_own_beside_them_still_saves(self, tmp_path: Path) -> None:
        path = _file(tmp_path)
        save_tier_slug("team", "worker", "new-worker", path)
        save_tier_values("team", "worker", {"context_window": 65536}, path=path)
        loaded = load_config(path)
        assert loaded.presets["team"].worker.slug == "new-worker"
        assert loaded.presets["team"].worker.context_window == 65536
        # The anchored roles did not move.
        assert loaded.presets["main"].worker.slug == "main-model"


def _docs_examples() -> list[str]:
    docs = Path(__file__).resolve().parents[2] / "docs" / "models.md"
    text = docs.read_text(encoding="utf-8")
    blocks = [b.split("\n", 1)[1] for b in text.split("```yaml")[1:]]
    return [b.split("```", 1)[0] for b in blocks if "presets:" in b]


@pytest.mark.parametrize("example", _docs_examples())
def test_the_card_refresh_never_breaks_a_docs_example(tmp_path: Path, example: str) -> None:
    """SETMODEL-01: the background refresh writes over every role it can."""
    path = tmp_path / "config.yaml"
    path.write_text(example, encoding="utf-8")
    try:
        config = load_config(path)
    except ConfigError:
        pytest.skip("a fragment, not a whole file")
    for name, preset in config.presets.items():
        for tier in preset.tier_map():
            before = path.read_bytes()
            try:
                save_tier_values(name, tier, {"context_window": 123456}, path=path)
            except ConfigError:
                assert path.read_bytes() == before
            load_config(path)


class TestRemovingAKey:
    LOCAL_AND_HOSTED = """\
credentials:
  acme:
    name: Acme
    base_url: https://models.example.com/v1
  lab:
    name: Lab
presets:
  hosted:
    label: Acme Sonnet
    brain: &hosted_tier
      base_url: https://models.example.com/v1
      slug: acme-sonnet
      credential: acme
      context_window: 32768
      max_output_tokens: 4096
      input_price: 1.0
      output_price: 1.0
      cache_read_price: 0.1
    worker: *hosted_tier
    validator: *hosted_tier
  local:
    label: Local
    brain:
      base_url: http://127.0.0.1:8000/v1
      slug: lab-model
      credential: lab
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker:
      base_url: http://127.0.0.1:8000/v1
      slug: lab-model
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    validator:
      base_url: http://127.0.0.1:8000/v1
      slug: lab-model
      credential: lab
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
active_preset: local
"""

    def _daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Daemon, Path, dict[str, str]]:
        path = tmp_path / "config.yaml"
        path.write_text(self.LOCAL_AND_HOSTED, encoding="utf-8")
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: load_config(path))
        secrets = {"acme": "sk-acme", "lab": "sk-lab"}  # tst-secret-ok

        async def _delete(provider_name: str = "openrouter") -> None:
            del secrets[provider_name]

        monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
        daemon = Daemon(data_dir=tmp_path)
        daemon.config_path = path
        return daemon, path, secrets

    async def _remove(self, daemon: Daemon, credential: str) -> dict[str, Any]:
        msg = {"type": "delete_credential", "credential": credential}
        raw = await daemon._handle_message(json.dumps(msg), None)
        assert raw is not None
        return dict(json.loads(raw))

    async def test_a_key_a_hosted_model_uses_is_refused_and_nothing_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, path, secrets = self._daemon(tmp_path, monkeypatch)
        before = path.read_bytes()
        reply = await self._remove(daemon, "acme")
        assert (reply["type"], reply["code"]) == ("error", "bad_request")
        assert reply["message"] == (
            "Acme Sonnet uses this key. Remove that model or give it another key first."
        )
        assert path.read_bytes() == before and "acme" in secrets

    async def test_a_key_only_a_local_model_uses_is_unbound_then_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, path, secrets = self._daemon(tmp_path, monkeypatch)
        reply = await self._remove(daemon, "lab")
        assert reply["type"] == "setup_state", reply
        loaded = load_config(path)  # the file still loads: the daemon can start
        assert "lab" not in loaded.credentials
        assert loaded.presets["local"].brain.credential is None
        assert loaded.presets["local"].validator.credential is None
        assert "lab" not in secrets and "acme" in secrets
