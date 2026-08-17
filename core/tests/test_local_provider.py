"""Keyless local provider (TD-1801).

A loopback ``base_url`` is on-box, so there is no third party to
authenticate against: the daemon resolves a provider client for it without
consulting the keychain, and the client sends no ``Authorization`` header.
Remote endpoints keep the keychain requirement — the negative test is the
point of the story, not an afterthought.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tstd.config import ModelConfig, default_config_yaml, is_loopback_url
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.provider import (
    ChatCompletionRequest,
    ChatMessage,
    ProviderClient,
    RetryConfig,
)

# ── Helpers ────────────────────────────────────────────────────────────

_MISSING_KEY = "API key not found in keychain."


def _config_with_preset(name: str) -> ModelConfig:
    """The shipped config forced onto *name*.

    Read through the real loader so no slug or provider URL is duplicated
    into test source (§2.7), and so a retargeted preset is picked up here
    without editing the test.
    """
    config = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    return config.model_copy(update={"active_preset": name})


def _daemon(monkeypatch: pytest.MonkeyPatch, data_dir: Path, preset: str) -> Daemon:
    monkeypatch.setattr("tstd.daemon.cached_config", lambda: _config_with_preset(preset))
    return Daemon(data_dir=data_dir)


# What an endpoint serving exactly one model would resolve to (TD-1809).
_RESOLVED_SLUG = "local-model"


async def _resolve_one_model(config: ModelConfig, **_: Any) -> None:
    """Stand in for a single-model endpoint, without reaching for one.

    ``_provider_probe`` resolves slugs before it builds a client, and the
    shipped ``local`` preset sets none, so an unpatched probe discovers
    against the real loopback endpoint — handing the verdict to whatever
    models the developer has pulled.  ``discover_model`` refuses to guess
    among several (TD-1805), which is right, and made this row fail for a
    reason that has nothing to do with keyless auth.
    """
    for tier in config.tiers().values():
        if tier.slug is None:
            tier.slug = _RESOLVED_SLUG


def _completion_body(model: str) -> dict[str, Any]:
    return {
        "id": "keyless-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def keychain_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """An empty keychain that records every read.

    Both the daemon's presence probe and the provider's deferred import are
    patched, so a keychain read cannot escape to the developer's real login
    keychain during the suite.
    """
    calls: list[str] = []

    async def _get(provider_name: str = "openrouter") -> str:
        calls.append(provider_name)
        raise KeychainError(_MISSING_KEY)

    monkeypatch.setattr("tstd.keychain.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    return calls


class _FakeClient:
    """Provider stand-in whose probe always succeeds."""

    def __init__(self, base_url: str = "", api_key: str | None = None, **kwargs: Any) -> None:
        self.base_url = base_url
        self.api_key = api_key

    @classmethod
    async def from_keychain(cls, base_url: str) -> _FakeClient:
        raise AssertionError("keychain consulted for a loopback endpoint")

    async def chat_completion(self, request: Any) -> Any:
        return object()  # non-ProviderError ⇒ the probe succeeded


# ── The predicate ──────────────────────────────────────────────────────


class TestIsLoopbackUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434/v1",
            "http://127.0.0.1/v1",
            "https://127.0.0.1:8443/v1",
            "http://127.0.0.2:8000/v1",
            "http://127.255.255.254/v1",
            "http://localhost:8000/v1",
            "http://localhost/v1",
            "http://LOCALHOST:8000/v1",
            "http://[::1]:11434/v1",
            "http://[::1]/v1",
        ],
    )
    def test_loopback_hosts(self, url: str) -> None:
        assert is_loopback_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://openrouter.ai/api/v1",
            "https://api.example.com/v1",
            "http://128.0.0.1/v1",
            "http://126.255.255.255/v1",
            # A LAN address is on the network, not on this box.
            "http://10.0.0.5:8000/v1",
            "http://192.168.1.10:11434/v1",
            # Binding to all interfaces is not the same as calling ourselves.
            "http://0.0.0.0:8000/v1",
            # Names that merely start or end with the magic word.
            "http://localhost.example.com/v1",
            "http://notlocalhost/v1",
            "http://[::2]/v1",
            # Unclassifiable input keeps the key requirement.
            "localhost:8000/v1",
            "http://[::1/v1",
            "",
        ],
    )
    def test_remote_or_unclassifiable_hosts(self, url: str) -> None:
        assert is_loopback_url(url) is False


class TestRequiresApiKey:
    def test_local_preset_needs_no_key(self) -> None:
        assert _config_with_preset("local").requires_api_key() is False

    def test_shipped_default_preset_needs_a_key(self) -> None:
        assert _config_with_preset("tst-default").requires_api_key() is True

    def test_one_remote_tier_keeps_the_requirement(self) -> None:
        """A mixed preset is not keyless — the off-box tier decides."""
        config = _config_with_preset("local")
        remote = _config_with_preset("tst-default").tier("worker")
        config.presets["local"].worker = remote
        assert config.requires_api_key() is True


# ── Resolution ─────────────────────────────────────────────────────────


class TestEnsureProvider:
    async def test_loopback_resolves_with_no_keychain_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "local")
        provider = await daemon._ensure_provider()
        try:
            assert isinstance(provider, ProviderClient)
            assert provider.api_key is None
            assert keychain_calls == []
        finally:
            await provider.close()

    async def test_resolution_is_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "local")
        first = await daemon._ensure_provider()
        try:
            assert await daemon._ensure_provider() is first
        finally:
            assert isinstance(first, ProviderClient)
            await first.close()

    async def test_remote_tier_still_requires_a_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        """The negative case: a keychain outage must not degrade a remote
        endpoint into an unauthenticated call."""
        daemon = _daemon(monkeypatch, tmp_path, "tst-default")
        with pytest.raises(KeychainError, match="not found"):
            await daemon._ensure_provider()
        assert keychain_calls == ["openrouter"]


class TestAuthorizationHeader:
    async def _capture(self, api_key: str | None) -> httpx.Request:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_completion_body("local-model"))

        client = ProviderClient(
            base_url="http://127.0.0.1:11434/v1",
            api_key=api_key,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            retry_config=RetryConfig(max_retries=0),
        )
        try:
            await client.chat_completion(
                ChatCompletionRequest(
                    model="local-model",
                    messages=[ChatMessage(role="user", content="ok")],
                    stream=False,
                )
            )
        finally:
            await client.close()
        return seen[0]

    async def test_keyless_client_sends_no_authorization(self) -> None:
        request = await self._capture(None)
        assert "authorization" not in request.headers

    async def test_keyed_client_still_authenticates(self) -> None:
        request = await self._capture("sk-test-key")  # tst-secret-ok
        assert request.headers["authorization"] == "Bearer sk-test-key"


# ── Onboarding and diagnostics surfaces ────────────────────────────────


class TestSetupState:
    async def test_local_preset_reports_no_key_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "local")
        event = await daemon._setup_state_event()
        assert event.has_api_key is False
        assert event.key_required is False

    async def test_remote_preset_still_requires_a_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "tst-default")
        event = await daemon._setup_state_event()
        assert event.has_api_key is False
        assert event.key_required is True


class TestDoctorRows:
    async def test_local_preset_does_not_fail_the_key_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        monkeypatch.setattr("tstd.daemon.ProviderClient", _FakeClient)
        monkeypatch.setattr("tstd.daemon.resolve_tier_slugs", _resolve_one_model)
        daemon = _daemon(monkeypatch, tmp_path, "local")
        report = await daemon._diagnostics_report()
        rows = {c.name: c for c in report.checks}
        assert rows["api_key"].status == "skip"
        assert "not needed" in rows["api_key"].detail
        # The endpoint was still probed — keyless is not unchecked.
        assert rows["provider"].status == "ok"

    async def test_remote_preset_with_no_key_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keychain_calls: list[str]
    ) -> None:
        monkeypatch.setattr("tstd.daemon.ProviderClient", _FakeClient)
        monkeypatch.setattr("tstd.daemon.resolve_tier_slugs", _resolve_one_model)
        daemon = _daemon(monkeypatch, tmp_path, "tst-default")
        report = await daemon._diagnostics_report()
        rows = {c.name: c for c in report.checks}
        assert rows["api_key"].status == "fail"
        assert rows["provider"].status == "skip"
