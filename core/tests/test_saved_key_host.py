"""A saved key goes only to its own server (TD-5182).

A client can name a saved key by id in ``list_endpoint_models``,
``add_model`` and ``validate_api_key``, and name one to remove in
``delete_api_key``. The id must be a saved API key, never another keychain
item, and the key may go only where config already sends it: the host saved
with it, a model bound to it, or, for a key saved without a host, a server on
this computer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd import daemon as daemon_module
from tstd.config import CredentialConfig, ModelConfig, Preset, TierConfig, load_config
from tstd.config_write import save_credential
from tstd.key_hosts import is_key_id, key_hosts, key_refusal, origin
from tstd.keychain import KeychainError
from tstd.mock import MockProvider, Script
from tstd.protocol import EndpointModel, EndpointModels
from tstd.provider import ProviderClient

OPENROUTER = "https://openrouter.ai/api/v1"
OTHER_KEYCHAIN_ITEMS = (
    "github-oauth",
    "ntfy-topic",
    "slack-webhook",
    "mcp.github.github_token",
    "machine.desk-2",
)


def _tier(base_url: str, credential: str | None = None) -> TierConfig:
    return TierConfig(
        slug="m1",
        base_url=base_url,
        credential=credential,
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=64,
    )


def _config() -> ModelConfig:
    lan = _tier("http://10.0.0.5:8000/v1", credential="box")
    hosted = _tier(OPENROUTER, credential="openrouter").model_copy(
        update={"fallbacks": [_tier("http://10.0.0.9:8000/v1", credential="backup")]}
    )
    return ModelConfig(
        presets={
            "hosted": Preset(brain=hosted, worker=hosted, validator=hosted),
            "lan": Preset(brain=lan, worker=lan, validator=lan),
        },
        active_preset="hosted",
        credentials={
            "openrouter": CredentialConfig(name="OpenRouter", base_url=OPENROUTER),
            "box": CredentialConfig(name="LAN box"),
            "local": CredentialConfig(name="Local server"),
            "backup": CredentialConfig(name="Backup box"),
        },
    )


class TestRule:
    def test_a_hosted_key_goes_to_its_own_host(self) -> None:
        cfg = _config()
        assert key_refusal(cfg, "openrouter", OPENROUTER) is None
        assert key_refusal(cfg, "openrouter", OPENROUTER + "/") is None
        assert key_refusal(cfg, "openrouter", "https://OpenRouter.ai/other/path") is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/v1",
            "http://openrouter.ai/api/v1",
            "https://openrouter.ai:8443/api/v1",
            "http://127.0.0.1:11434/v1",
            "not a url",
        ],
    )
    def test_a_hosted_key_goes_nowhere_else(self, url: str) -> None:
        refusal = key_refusal(_config(), "openrouter", url)
        assert refusal is not None
        assert OPENROUTER in refusal

    def test_a_key_without_a_host_goes_to_its_models_or_this_computer(self) -> None:
        cfg = _config()
        assert key_hosts(cfg, "box") == ["http://10.0.0.5:8000/v1"]
        assert key_refusal(cfg, "box", "http://10.0.0.5:8000/v1") is None
        assert key_refusal(cfg, "box", "http://127.0.0.1:8000/v1") is None
        refusal = key_refusal(cfg, "box", "https://evil.example/v1")
        assert refusal is not None and "http://10.0.0.5:8000/v1" in refusal

    def test_a_key_only_a_backup_uses_goes_to_that_backup(self) -> None:
        cfg = _config()
        assert key_hosts(cfg, "backup") == ["http://10.0.0.9:8000/v1"]
        assert key_refusal(cfg, "backup", "http://10.0.0.9:8000/v1") is None
        assert key_refusal(cfg, "backup", "http://10.0.0.5:8000/v1") is not None

    def test_an_unused_key_without_a_host_stays_on_this_computer(self) -> None:
        cfg = _config()
        assert key_hosts(cfg, "local") == []
        assert key_refusal(cfg, "local", "http://localhost:1234/v1") is None
        refusal = key_refusal(cfg, "local", "http://10.0.0.5:8000/v1")
        assert refusal is not None and "a server on this computer" in refusal

    @pytest.mark.parametrize("name", [*OTHER_KEYCHAIN_ITEMS, "never-saved", ""])
    def test_only_a_saved_key_can_be_named(self, name: str) -> None:
        refusal = key_refusal(_config(), name, OPENROUTER)
        assert refusal is not None and "no saved key" in refusal

    def test_key_ids(self) -> None:
        assert is_key_id("openrouter") and is_key_id("box-2")
        assert not any(is_key_id(name) for name in OTHER_KEYCHAIN_ITEMS)

    def test_origin(self) -> None:
        assert origin("https://a.example/v1") == ("https", "a.example", 443)
        assert origin("http://[::1]:8000/v1") == ("http", "::1", 8000)
        assert origin("ftp://a.example") is None
        assert origin("http://a.example:notaport/") is None


# ── Through the daemon ────────────────────────────────────────────────────


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Records keychain reads, deletes, model lists and probes; sends nothing."""
    calls: dict[str, list[Any]] = {"read": [], "delete": [], "list": [], "probe": []}

    async def read(name: str = "openrouter") -> str:
        calls["read"].append(name)
        return "sk-saved"

    async def delete(name: str = "openrouter") -> None:
        calls["delete"].append(name)

    async def listing(base_url: str, api_key: str | None, **_: Any) -> EndpointModels:
        calls["list"].append((base_url, api_key))
        return EndpointModels(base_url=base_url, models=[EndpointModel(id="m1")])

    async def probe(base_url: str, **kwargs: Any) -> Any:
        calls["probe"].append((base_url, kwargs.get("provider_name")))
        raise KeychainError("stopped before sending")

    monkeypatch.setattr(daemon_module, "get_api_key", read)
    monkeypatch.setattr(daemon_module, "delete_api_key", delete)
    monkeypatch.setattr(daemon_module, "list_endpoint_models", listing)
    monkeypatch.setattr(ProviderClient, "from_keychain", probe)
    return calls


def _data_dir(tmp_path: Path) -> Path:
    """The test catalog plus a spare hosted key and a local-server key."""
    path = tmp_path / "config.yaml"
    path.write_text(catalog_config_yaml(), encoding="utf-8")
    save_credential("spare", "Spare", path=path, base_url="https://spare.example/v1")
    save_credential("local", "Local server", path=path)
    cfg = load_config(path)
    assert "spare" in cfg.credentials and cfg.credentials["local"].base_url is None
    return tmp_path


async def _session(tmp_path: Path) -> tuple[Any, Any]:
    _daemon, task, ws = await _start(_data_dir(tmp_path), MockProvider(default=Script(kind="text")))
    return task, ws


class TestDaemon:
    async def test_a_listing_never_sends_a_saved_key_elsewhere(
        self, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            msg = {
                "type": "list_endpoint_models",
                "base_url": "https://evil.example/v1",
                "credential": "openrouter",
            }
            reply = await _ask(ws, msg, "endpoint_models")
            assert reply["type"] == "endpoint_models"
            assert "so it is not sent anywhere else" in reply["error"]
            assert seen["read"] == [] and seen["list"] == []
        finally:
            await _stop(task, ws)

    @pytest.mark.parametrize("name", OTHER_KEYCHAIN_ITEMS)
    async def test_a_listing_cannot_name_another_keychain_item(
        self, name: str, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            msg = {"type": "list_endpoint_models", "base_url": OPENROUTER, "credential": name}
            reply = await _ask(ws, msg, "endpoint_models")
            assert "no saved key" in reply["error"]
            assert seen["read"] == [] and seen["list"] == []
        finally:
            await _stop(task, ws)

    async def test_a_listing_at_the_keys_own_host_uses_it(
        self, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            msg = {
                "type": "list_endpoint_models",
                "base_url": "https://spare.example/v1",
                "credential": "spare",
            }
            reply = await _ask(ws, msg, "endpoint_models")
            assert reply.get("error") is None, reply
            assert seen["read"] == ["spare"]
            assert seen["list"] == [("https://spare.example/v1", "sk-saved")]
        finally:
            await _stop(task, ws)

    async def test_a_model_cannot_bind_a_saved_key_elsewhere(
        self, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            msg = {
                "type": "add_model",
                "label": "Elsewhere",
                "base_url": "https://evil.example/v1",
                "slug": "vendor/model",
                "credential": "spare",
            }
            reply = await _ask(ws, msg, "setup_state")
            assert reply["type"] == "error" and reply["code"] == "bad_request"
            assert "https://spare.example/v1" in reply["message"]
            assert "elsewhere" not in load_config(tmp_path / "config.yaml").presets
        finally:
            await _stop(task, ws)

    async def test_a_key_test_goes_to_the_keys_own_host(
        self, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        """No model uses "spare", so the test goes to its host, not the main model's."""
        task, ws = await _session(tmp_path)
        try:
            msg = {"type": "validate_api_key", "credential": "spare"}
            reply = await _ask(ws, msg, "api_key_validated")
            assert reply["ok"] is False
            assert seen["probe"] == [("https://spare.example/v1", "spare")]
        finally:
            await _stop(task, ws)

    @pytest.mark.parametrize("name", ["github-oauth", "local"])
    async def test_a_key_test_with_nowhere_to_go_sends_nothing(
        self, name: str, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            msg = {"type": "validate_api_key", "credential": name}
            reply = await _ask(ws, msg, "api_key_validated")
            assert reply["ok"] is False
            assert "no saved key" in reply["detail"] or "No model uses" in reply["detail"]
            assert seen["probe"] == [] and seen["read"] == []
        finally:
            await _stop(task, ws)

    @pytest.mark.parametrize("name", ["github-oauth", "mcp.github.github_token"])
    async def test_removing_a_key_cannot_remove_another_keychain_item(
        self, name: str, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            reply = await _ask(ws, {"type": "delete_api_key", "provider": name}, "setup_state")
            assert reply["type"] == "error" and reply["code"] == "bad_request"
            assert seen["delete"] == []
        finally:
            await _stop(task, ws)

    async def test_removing_a_saved_key_still_works(
        self, tmp_path: Path, seen: dict[str, list[Any]]
    ) -> None:
        task, ws = await _session(tmp_path)
        try:
            reply = await _ask(ws, {"type": "delete_api_key", "provider": "spare"}, "setup_state")
            assert reply["type"] == "setup_state", reply
            assert seen["delete"] == ["spare"]
        finally:
            await _stop(task, ws)
