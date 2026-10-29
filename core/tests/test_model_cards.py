"""Model-card refresh (TD-5185): prices and limits from the tier's own host.

Every request here goes to an ``httpx.MockTransport``; nothing leaves the
machine. The card fixture is shaped like OpenRouter's ``/api/v1/models``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd import daemon as daemon_module
from tstd import model_cards
from tstd.card_write import format_value, save_tier_values
from tstd.config import (
    ConfigError,
    CredentialConfig,
    ModelConfig,
    Preset,
    TierConfig,
    load_config,
)
from tstd.mock import MockProvider, Script
from tstd.model_cards import card_values, refresh_cards, targets
from tstd.model_setup import list_endpoint_models, parse_model_list
from tstd.protocol import EndpointModel, EndpointModels

OPENROUTER = "https://openrouter.ai/api/v1"
LOCAL = "http://127.0.0.1:8000/v1"
KEY = "sk-or-test-never-logged"

CARD = {
    "id": "moonshotai/kimi-k3",
    "context_length": 1048576,
    "pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002"},
    "top_provider": {"max_completion_tokens": 65536},
}
NO_CACHE_PRICE = {
    "id": "deepseek/deepseek-v4-flash",
    "context_length": 163840,
    "pricing": {"prompt": "0.00000007", "completion": "0.00000017"},
}
LISTING = {"data": [CARD, NO_CACHE_PRICE]}
# What a local server (vLLM, Ollama) answers: ids and a ceiling, no prices.
LOCAL_LISTING = {"data": [{"id": "qwen3.8:27b", "max_model_len": 131072}]}


def _tier(
    slug: str | None,
    base_url: str = OPENROUTER,
    credential: str | None = "openrouter",
    **extra: Any,
) -> TierConfig:
    fields: dict[str, Any] = {
        "slug": slug,
        "base_url": base_url,
        "credential": credential,
        "input_price": 9.0,
        "output_price": 9.0,
        "cache_read_price": 9.0,
        "context_window": 32768,
        "max_output_tokens": 8192,
    }
    fields.update(extra)
    return TierConfig(**fields)


def _config(
    brain: TierConfig, other: TierConfig | None = None, **keys: CredentialConfig
) -> ModelConfig:
    rest = other or brain
    return ModelConfig(
        presets={"p": Preset(brain=brain, worker=rest, validator=rest)},
        active_preset="p",
        credentials={
            "openrouter": CredentialConfig(name="OpenRouter", base_url=OPENROUTER),
            **keys,
        },
    )


class Hosts:
    """A MockTransport that answers ``/models`` per host and records requests."""

    def __init__(self, bodies: dict[str, Any], fail: set[str] | None = None) -> None:
        self.bodies = bodies
        self.fail = fail or set()
        self.requests: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("Authorization")))
        base = url.removesuffix("/models")
        if base in self.fail:
            raise httpx.ConnectError("refused", request=request)
        if base not in self.bodies:
            return httpx.Response(404)
        return httpx.Response(200, json=self.bodies[base])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _read_key(_credential: str) -> str:
    return KEY


async def _refresh(config: ModelConfig, hosts: Hosts, tier: str | None = None) -> list[Any]:
    async with hosts.client() as client:
        return await refresh_cards(config, targets(config, "p", tier), _read_key, client=client)


# ── Parsing and mapping ───────────────────────────────────────────────────


class TestMapping:
    def test_the_card_is_read_in_dollars_per_million(self) -> None:
        rows = parse_model_list(LISTING)
        assert rows is not None
        card = next(row for row in rows if row.id == CARD["id"])
        assert card.input_price == 2.0
        assert card.output_price == 10.0
        assert card.cache_read_price == 0.2
        assert card.context_window == 1048576
        assert card.max_output_tokens == 65536

    def test_a_full_card_sets_every_field(self) -> None:
        card = EndpointModel(
            id="m",
            input_price=2.0,
            output_price=10.0,
            cache_read_price=0.2,
            context_window=1048576,
            max_output_tokens=65536,
        )
        assert card_values(_tier("m"), card, loopback=False) == {
            "input_price": 2.0,
            "output_price": 10.0,
            "cache_read_price": 0.2,
            "context_window": 1048576,
        }

    def test_a_missing_cache_price_is_left_out_not_zeroed(self) -> None:
        card = EndpointModel(id="m", input_price=0.07, output_price=0.17, context_window=163840)
        values = card_values(_tier("m"), card, loopback=False)
        assert "cache_read_price" not in values
        assert "max_output_tokens" not in values

    def test_loopback_keeps_its_hand_set_window(self) -> None:
        card = EndpointModel(id="m", context_window=131072, max_output_tokens=4096)
        values = card_values(_tier("m", LOCAL, None), card, loopback=True)
        assert "context_window" not in values
        # 4096 is under the stored 8192, so the ceiling still applies.
        assert values == {"max_output_tokens": 4096}

    def test_the_output_cap_only_ever_lowers(self) -> None:
        # A provider ceiling of 943718 must not become every request's
        # max_tokens; a ceiling under the stored value is a real limit.
        card = EndpointModel(id="m", context_window=1048576, max_output_tokens=943718)
        assert "max_output_tokens" not in card_values(_tier("m"), card, loopback=False)
        card = EndpointModel(id="m", context_window=1048576, max_output_tokens=4096)
        assert card_values(_tier("m"), card, loopback=False)["max_output_tokens"] == 4096

    def test_an_output_cap_never_eats_the_window(self) -> None:
        card = EndpointModel(id="m", context_window=131072, max_output_tokens=65536)
        values = card_values(_tier("m", LOCAL, None), card, loopback=True)
        assert "max_output_tokens" not in values

    def test_an_output_cap_equal_to_the_context_is_ignored(self) -> None:
        card = EndpointModel(id="m", context_window=8192, max_output_tokens=8192)
        assert "max_output_tokens" not in card_values(_tier("m"), card, loopback=False)

    def test_a_negative_router_price_is_no_price(self) -> None:
        rows = parse_model_list({"data": [{"id": "openrouter/auto", "pricing": {"prompt": "-1"}}]})
        assert rows is not None and rows[0].input_price is None


# ── Refresh against a host ────────────────────────────────────────────────


class TestRefresh:
    async def test_a_listed_slug_matches_exactly(self) -> None:
        hosts = Hosts({OPENROUTER: LISTING})
        results = await _refresh(_config(_tier(CARD["id"])), hosts, "brain")
        [result] = results
        assert result.status == "ok"
        assert result.values["input_price"] == 2.0
        assert result.values["context_window"] == 1048576
        assert hosts.requests == [(f"{OPENROUTER}/models", f"Bearer {KEY}")]

    async def test_a_slug_not_listed_changes_nothing(self) -> None:
        hosts = Hosts({OPENROUTER: LISTING})
        # A near miss must not match: no prefix or fuzzy pick.
        [result] = await _refresh(_config(_tier("moonshotai/kimi-k3-mini")), hosts, "brain")
        assert result.status == "not_listed"
        assert result.values == {}

    async def test_a_host_without_prices_leaves_the_prices(self) -> None:
        box = "http://10.0.0.5:8000/v1"
        config = _config(
            _tier("qwen3.8:27b", box, "box"), box=CredentialConfig(name="LAN box", base_url=box)
        )
        hosts = Hosts({box: LOCAL_LISTING})
        [result] = await _refresh(config, hosts, "brain")
        assert result.status == "no_pricing"
        assert not {"input_price", "output_price", "cache_read_price"} & set(result.values)
        # Off loopback the served max_model_len is the real window.
        assert result.values == {"context_window": 131072}

    async def test_a_loopback_host_keeps_its_window_and_sends_no_key(self) -> None:
        hosts = Hosts({LOCAL: LOCAL_LISTING})
        [result] = await _refresh(_config(_tier("qwen3.8:27b", LOCAL, None)), hosts, "brain")
        assert result.status == "no_pricing"
        assert result.values == {}
        assert hosts.requests == [(f"{LOCAL}/models", None)]

    async def test_a_loopback_url_with_a_hosted_key_is_the_hosted_host(self) -> None:
        # The live `vllm` preset's shape: tier URL on this computer, key on
        # OpenRouter. Turns go to OpenRouter, so the card does too, and the
        # window is the hosted one.
        tier = _tier(CARD["id"], LOCAL, "openrouter")
        hosts = Hosts({OPENROUTER: LISTING})
        [result] = await _refresh(_config(tier), hosts, "brain")
        assert result.host == OPENROUTER
        assert result.status == "ok"
        assert result.values["context_window"] == 1048576
        assert [url for url, _ in hosts.requests] == [f"{OPENROUTER}/models"]

    async def test_a_backup_is_refreshed_from_its_own_card(self) -> None:
        backup = _tier(NO_CACHE_PRICE["id"])
        brain = _tier(CARD["id"], fallbacks=[backup])
        hosts = Hosts({OPENROUTER: LISTING})
        results = await _refresh(_config(brain), hosts, "brain")
        primary, spare = results
        assert (primary.target.fallback, spare.target.fallback) == (None, 0)
        assert primary.values["input_price"] == 2.0
        assert spare.values["input_price"] == 0.07
        assert "cache_read_price" not in spare.values
        # One host, one request, for both.
        assert len(hosts.requests) == 1

    async def test_a_failed_fetch_is_reported_and_changes_nothing(self) -> None:
        hosts = Hosts({}, fail={OPENROUTER})
        [result] = await _refresh(_config(_tier(CARD["id"])), hosts, "brain")
        assert result.status == "fetch_failed"
        assert result.values == {}
        assert result.detail and "No server answered" in result.detail

    async def test_a_tier_with_no_slug_asks_nobody(self) -> None:
        hosts = Hosts({LOCAL: LOCAL_LISTING})
        [result] = await _refresh(_config(_tier(None, LOCAL, None)), hosts, "brain")
        assert result.status == "no_slug"
        assert hosts.requests == []

    async def test_every_tier_when_none_is_named(self) -> None:
        hosts = Hosts({OPENROUTER: LISTING})
        config = _config(_tier(CARD["id"]), _tier(NO_CACHE_PRICE["id"]))
        results = await _refresh(config, hosts)
        assert [r.target.tier for r in results] == ["brain", "worker", "validator"]
        assert len(hosts.requests) == 1

    def test_an_unknown_preset_or_tier_is_refused(self) -> None:
        config = _config(_tier("m"))
        with pytest.raises(ConfigError):
            targets(config, "nope")
        with pytest.raises(ConfigError):
            targets(config, "p", "boss")


# ── Writing config.yaml ───────────────────────────────────────────────────

CONFIG_TEXT = """\
# top comment
presets:
  p:
    brain:
      slug: "moonshotai/kimi-k3"
      base_url: https://openrouter.ai/api/v1
      credential: openrouter
      # Prices are dollars per million tokens.
      input_price: 0.0   # was free
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 8192
      fallbacks:
        - slug: "deepseek/deepseek-v4-flash"
          base_url: https://openrouter.ai/api/v1
          credential: openrouter
          input_price: 5.0
          output_price: 5.0
          cache_read_price: 5.0
          context_window: 32768
          max_output_tokens: 8192
    worker:
      slug: "deepseek/deepseek-v4-flash"
      base_url: https://openrouter.ai/api/v1
      credential: openrouter
      input_price: 1.0
      output_price: 1.0
      cache_read_price: 1.0
      context_window: 32768
      max_output_tokens: 8192
"""


class TestWrite:
    def test_values_are_replaced_and_comments_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG_TEXT, encoding="utf-8")
        save_tier_values("p", "brain", {"input_price": 2.0, "context_window": 1048576}, path=path)
        text = path.read_text(encoding="utf-8")
        assert "      input_price: 2.0   # was free" in text
        assert "# Prices are dollars per million tokens." in text
        assert "# top comment" in text
        data = yaml.safe_load(text)
        brain = data["presets"]["p"]["brain"]
        assert brain["input_price"] == 2.0 and brain["context_window"] == 1048576
        # The backup and the worker are untouched.
        assert brain["fallbacks"][0]["input_price"] == 5.0
        assert data["presets"]["p"]["worker"]["input_price"] == 1.0
        assert brain["fallbacks"][0]["context_window"] == 32768

    def test_a_backup_is_written_inside_its_own_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG_TEXT, encoding="utf-8")
        save_tier_values("p", "brain", {"input_price": 0.07}, fallback=0, path=path)
        brain = yaml.safe_load(path.read_text(encoding="utf-8"))["presets"]["p"]["brain"]
        assert brain["input_price"] == 0.0
        assert brain["fallbacks"][0]["input_price"] == 0.07

    def test_a_missing_key_is_added_at_the_tier_indent(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        trimmed = CONFIG_TEXT.removesuffix("      max_output_tokens: 8192\n")
        assert trimmed != CONFIG_TEXT
        path.write_text(trimmed, encoding="utf-8")
        save_tier_values("p", "worker", {"max_output_tokens": 16384}, path=path)
        worker = yaml.safe_load(path.read_text(encoding="utf-8"))["presets"]["p"]["worker"]
        assert worker["max_output_tokens"] == 16384

    def test_a_missing_backup_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG_TEXT, encoding="utf-8")
        with pytest.raises(ConfigError):
            save_tier_values("p", "worker", {"input_price": 1.5}, fallback=0, path=path)

    @pytest.mark.parametrize(
        ("value", "text"),
        [(2.0, "2.0"), (0.2, "0.2"), (0.000001, "0.000001"), (0.07, "0.07"), (1048576, "1048576")],
    )
    def test_numbers_read_back_as_numbers(self, value: float, text: str) -> None:
        assert format_value(value) == text
        assert yaml.safe_load(f"v: {text}")["v"] == value


# ── Through the daemon ────────────────────────────────────────────────────


@pytest.fixture
def card_host(monkeypatch: pytest.MonkeyPatch) -> Hosts:
    """OpenRouter's list behind a MockTransport; the keychain returns KEY."""
    hosts = Hosts({OPENROUTER: {"data": [CARD, NO_CACHE_PRICE]}})

    async def listing(base_url: str, api_key: str | None, **_: Any) -> EndpointModels:
        async with hosts.client() as client:
            return await list_endpoint_models(base_url, api_key, client=client)

    async def read(name: str = "openrouter") -> str:
        return KEY

    monkeypatch.setattr(model_cards, "list_endpoint_models", listing)
    monkeypatch.setattr(daemon_module, "get_api_key", read)
    return hosts


def _data_dir(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(catalog_config_yaml(), encoding="utf-8")
    return tmp_path


class TestDaemon:
    async def test_saving_a_slug_writes_its_card(
        self, tmp_path: Path, card_host: Hosts, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        _daemon, task, ws = await _start(
            _data_dir(tmp_path), MockProvider(default=Script(kind="text"))
        )
        try:
            msg = {
                "type": "set_tier_slug",
                "preset": "tst-default",
                "tier": "brain",
                "slug": CARD["id"],
            }
            state = await _ask(ws, msg, "setup_state")
            assert state["type"] == "setup_state", state
            card = state["tier_cards"]["brain"]
            assert card["card"] == "ok"
            assert (card["input_price"], card["output_price"]) == (2.0, 10.0)
            assert card["cache_read_price"] == 0.2
            assert card["context_window"] == 1048576
            # The card's 65536 ceiling is above the stored 8192: kept.
            assert card["max_output_tokens"] == 8192
            # Only the saved tier was asked about; the worker shows config.
            assert state["tier_cards"]["worker"]["card"] is None
            brain = load_config(tmp_path / "config.yaml").presets["tst-default"].brain
            assert brain.input_price == 2.0 and brain.context_window == 1048576
        finally:
            await _stop(task, ws)
        assert KEY not in caplog.text
        assert KEY not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    async def test_refresh_reports_a_miss_and_keeps_the_value(
        self, tmp_path: Path, card_host: Hosts
    ) -> None:
        _daemon, task, ws = await _start(
            _data_dir(tmp_path), MockProvider(default=Script(kind="text"))
        )
        before = load_config(tmp_path / "config.yaml").presets["tst-default"]
        try:
            msg = {"type": "refresh_model_cards", "preset": "tst-default"}
            state = await _ask(ws, msg, "setup_state")
            assert state["type"] == "setup_state", state
            cards = state["tier_cards"]
            # tst-default's brain slug (kimi-k3) is listed; the worker's
            # card has no cache price, so that one value stays.
            assert cards["brain"]["card"] == "ok"
            assert cards["worker"]["card"] == "ok"
            assert cards["worker"]["cache_read_price"] == before.worker.cache_read_price
            assert cards["worker"]["context_window"] == 163840
            # The validator (deepseek-v4-pro) is not in the list: untouched.
            assert cards["validator"]["card"] == "not_listed"
            assert cards["validator"]["input_price"] == before.validator.input_price
            assert "does not list" in cards["validator"]["detail"]
            assert len(card_host.requests) == 1
        finally:
            await _stop(task, ws)

    async def test_a_bad_preset_is_an_error(self, tmp_path: Path, card_host: Hosts) -> None:
        _daemon, task, ws = await _start(
            _data_dir(tmp_path), MockProvider(default=Script(kind="text"))
        )
        try:
            reply = await _ask(ws, {"type": "refresh_model_cards", "preset": "nope"}, "setup_state")
            assert reply["type"] == "error"
            assert card_host.requests == []
        finally:
            await _stop(task, ws)
