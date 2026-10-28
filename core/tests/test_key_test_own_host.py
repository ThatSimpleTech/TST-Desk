"""Settings Test on a key no model uses (TD-5184).

Such a key is tested at its own host (TD-5182). The test first asks for a
model already set up on that server. With none, it borrows the main model's
name, which the server may not serve: an answer that the model is unknown
means the key got past sign-in, so the key is reported as accepted. A bad
key, or any answer about the key, still fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _ask, _stop
from tests.test_saved_key_host import OPENROUTER, _config, _session, _tier
from tstd.config import CredentialConfig, Preset
from tstd.key_hosts import key_got_past_sign_in, key_probe
from tstd.provider import ProviderClient, ProviderError

SPARE = "https://spare.example/v1"


def _with_spare(*, same_host_model: bool) -> Any:
    cfg = _config()
    credentials = {**cfg.credentials, "spare": CredentialConfig(name="Spare", base_url=SPARE)}
    presets = dict(cfg.presets)
    if same_host_model:
        there = _tier(SPARE, credential="other").model_copy(update={"slug": "spare/served"})
        presets["there"] = Preset(brain=there, worker=there, validator=there)
        credentials["other"] = CredentialConfig(name="Other", base_url=SPARE)
    return cfg.model_copy(update={"credentials": credentials, "presets": presets})


def _miss(status: int, message: str, code: str = "") -> ProviderError:
    return ProviderError(code=code or f"http_{status}", message=message, status_code=status)


class TestProbeTarget:
    def test_a_model_on_the_keys_server_is_asked_first(self) -> None:
        probe = key_probe(_with_spare(same_host_model=True), "spare")
        assert probe.tier is not None and probe.tier.slug == "spare/served"
        assert probe.tier.base_url == SPARE
        assert probe.borrowed is False and probe.host == SPARE

    def test_otherwise_the_main_models_name_is_borrowed(self) -> None:
        cfg = _with_spare(same_host_model=False)
        probe = key_probe(cfg, "spare")
        assert probe.tier is not None and probe.tier.base_url == SPARE
        assert probe.tier.slug == cfg.tier("brain").slug
        assert probe.borrowed is True and probe.host == SPARE

    def test_a_key_a_model_uses_goes_to_that_model(self) -> None:
        probe = key_probe(_config(), "openrouter")
        assert probe.tier is not None and probe.tier.base_url == OPENROUTER
        assert probe.borrowed is False and probe.host == ""

    def test_unknown_and_hostless_unused_keys_go_nowhere(self) -> None:
        assert key_probe(_config(), "github-oauth").tier is None
        assert "No model uses" in key_probe(_config(), "local").refusal


class TestSignInRule:
    @pytest.mark.parametrize(
        "err",
        [
            _miss(404, "The model `vendor/m` does not exist or you do not have access to it."),
            _miss(400, "vendor/m is not a valid model ID"),
            _miss(422, "Unknown model: vendor/m"),
        ],
    )
    def test_an_unknown_model_means_the_key_got_in(self, err: ProviderError) -> None:
        assert key_got_past_sign_in(err)

    @pytest.mark.parametrize(
        "err",
        [
            _miss(401, "Unauthorized", code="auth_failed"),
            _miss(403, "Forbidden"),
            _miss(400, "API key not valid. Please pass a valid API key."),
            _miss(404, "model not found for this API key"),
            _miss(402, "Insufficient credits for this model"),
            _miss(429, "Rate limit exceeded for model vendor/m"),
            _miss(400, "messages must not be empty"),
        ],
    )
    def test_anything_else_is_not_a_pass(self, err: ProviderError) -> None:
        assert not key_got_past_sign_in(err)


class _Answer:
    """A stand-in for the provider client: one scripted reply per request."""

    def __init__(self, base_url: str, reply: object) -> None:
        self.base_url = base_url
        self.reply = reply
        self.models: list[str] = []

    async def chat_completion(self, request: Any) -> object:
        self.models.append(request.model)
        return self.reply


class _Script:
    def __init__(self) -> None:
        self.reply: object = None
        self.made: list[_Answer] = []


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> _Script:
    script = _Script()

    async def from_keychain(base_url: str, **_: Any) -> _Answer:
        client = _Answer(base_url, script.reply)
        script.made.append(client)
        return client

    monkeypatch.setattr(ProviderClient, "from_keychain", from_keychain)
    return script


async def _test_spare(tmp_path: Path, answer: _Script, reply: object) -> dict[str, Any]:
    answer.reply = reply
    task, ws = await _session(tmp_path)
    try:
        msg = {"type": "validate_api_key", "credential": "spare"}
        return await _ask(ws, msg, "api_key_validated")
    finally:
        await _stop(task, ws)


class TestDaemon:
    async def test_a_good_key_on_a_server_without_the_model_passes(
        self, tmp_path: Path, answer: _Script
    ) -> None:
        reply = await _test_spare(tmp_path, answer, _miss(404, "The model `m` does not exist"))
        assert reply["ok"] is True, reply
        assert reply["detail"] == f"Key accepted by {SPARE}. No model uses it yet."
        assert [client.base_url for client in answer.made] == [SPARE]

    async def test_a_bad_key_still_fails(self, tmp_path: Path, answer: _Script) -> None:
        reply = await _test_spare(tmp_path, answer, _miss(401, "nope", code="auth_failed"))
        assert reply["ok"] is False

    async def test_a_bad_key_reported_as_a_400_still_fails(
        self, tmp_path: Path, answer: _Script
    ) -> None:
        reply = await _test_spare(tmp_path, answer, _miss(400, "API key not valid."))
        assert reply["ok"] is False
        assert "API key not valid" in reply["detail"]

    async def test_an_answer_names_the_keys_host(self, tmp_path: Path, answer: _Script) -> None:
        reply = await _test_spare(tmp_path, answer, object())
        assert reply["ok"] is True
        assert reply["detail"] == f"Key accepted by {SPARE}."
