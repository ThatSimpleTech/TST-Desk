"""Refresh a tier's prices and limits from its host's model card (TD-5185).

A tier's prices, context window and output cap are config, typed by hand.
They go stale, and a wrong or zero price makes the cost meter lie. Most
hosted endpoints publish them: OpenRouter's ``/models`` lists per-token
prices, the context length and the output cap for every model. This module
asks the host a tier already talks to for that list, finds the tier's exact
slug, and works out which config values to replace.

Runs only on a user action: saving a tier's slug, or Refresh in Settings.
Never on a chat turn, and the chat loop never reads a card: it keeps
reading config, so a host that is down never stalls a turn.

What it will not do, each a way a guess would land in config:

- pick a nearby model when the slug is not listed;
- write zeros, or anything, for a field the card does not carry;
- replace a loopback tier's ``context_window`` with the model's ceiling.
  A local server serves what it was started with, often far less, and the
  hand-set number is the truth;
- price a backup from its primary's card or the other way round. Each entry
  is refreshed from its own slug at its own host (TD-5120).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from .config import (
    TIER_NAMES,
    ConfigError,
    ModelConfig,
    TierConfig,
    is_loopback_url,
    resolve_base_url,
    resolve_credential_id,
)
from .key_hosts import key_refusal
from .keychain import KeychainError
from .model_setup import list_endpoint_models
from .protocol import EndpointModel, EndpointModels

CardStatus = Literal["ok", "not_listed", "no_pricing", "fetch_failed", "no_slug", "write_failed"]

# Every field a card can set. Prices are dollars per million tokens, the
# unit config stores.
CardValues = dict[str, float | int]

KeyReader = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class CardTarget:
    """One tier, or one backup of a tier, to refresh."""

    preset: str
    tier: str
    fallback: int | None
    config: TierConfig

    @property
    def label(self) -> str:
        where = f"presets.{self.preset}.{self.tier}"
        return where if self.fallback is None else f"{where}.fallbacks[{self.fallback}]"


@dataclass
class CardResult:
    """What a refresh found for one target and which values it would write."""

    target: CardTarget
    status: CardStatus
    host: str
    detail: str | None = None
    values: CardValues = field(default_factory=dict)


def card_values(tier: TierConfig, card: EndpointModel, *, loopback: bool) -> CardValues:
    """The config values *card* sets for *tier*. A field the card lacks is absent.

    ``context_window`` comes from the card only off loopback. The output cap
    is taken only when it is smaller than the context length, and smaller
    than the window the tier will have afterwards, so the compaction budget
    (window minus output reservation) never goes to zero or below.
    """
    values: CardValues = {}
    if card.input_price is not None:
        values["input_price"] = card.input_price
    if card.output_price is not None:
        values["output_price"] = card.output_price
    if card.cache_read_price is not None:
        values["cache_read_price"] = card.cache_read_price
    window = tier.context_window
    if not loopback and card.context_window is not None:
        values["context_window"] = card.context_window
        window = card.context_window
    cap = card.max_output_tokens
    if (
        cap is not None
        and card.context_window is not None
        and cap < card.context_window
        and cap < window
    ):
        values["max_output_tokens"] = cap
    return values


def changed_values(tier: TierConfig, values: CardValues) -> CardValues:
    """Only the values that differ from what *tier* has now."""
    return {key: value for key, value in values.items() if getattr(tier, key) != value}


def targets(config: ModelConfig, preset: str, tier: str | None = None) -> list[CardTarget]:
    """*preset*'s tiers (or the one named) and each one's backups."""
    entry = config.presets.get(preset)
    if entry is None:
        raise ConfigError(
            f"Unknown preset {preset!r}; declared presets: {', '.join(sorted(config.presets))}"
        )
    if tier is not None and tier not in TIER_NAMES:
        raise ConfigError(f"Unknown tier {tier!r}; tiers are: {', '.join(TIER_NAMES)}")
    names = [tier] if tier is not None else list(TIER_NAMES)
    found: list[CardTarget] = []
    for name in names:
        tier_cfg: TierConfig = getattr(entry, name)
        found.append(CardTarget(preset, name, None, tier_cfg))
        found.extend(
            CardTarget(preset, name, index, backup)
            for index, backup in enumerate(tier_cfg.fallbacks)
        )
    return found


def match(target: CardTarget, host: str, listing: EndpointModels) -> CardResult:
    """Turn one host's list into the result for *target*."""
    slug = target.config.slug
    if slug is None:
        return CardResult(target, "no_slug", host, "No model is named, so there is no card.")
    if listing.error is not None:
        return CardResult(target, "fetch_failed", host, listing.error)
    card = next((row for row in listing.models if row.id == slug), None)
    if card is None:
        return CardResult(target, "not_listed", host, f"{host} does not list {slug}.")
    values = card_values(target.config, card, loopback=is_loopback_url(host))
    if card.input_price is None and card.output_price is None:
        return CardResult(
            target, "no_pricing", host, f"{host} publishes no price for {slug}.", values
        )
    return CardResult(target, "ok", host, None, values)


async def refresh_cards(
    config: ModelConfig,
    wanted: list[CardTarget],
    read_key: KeyReader,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[CardResult]:
    """Fetch each host's list once and match every target against it.

    The key sent is the one the tier itself sends, and only to the host the
    tier itself calls, checked against the saved-key rule (TD-5182).
    """
    listings: dict[tuple[str, str | None], EndpointModels] = {}
    results: list[CardResult] = []
    for target in wanted:
        host = resolve_base_url(config, target.config).rstrip("/")
        if target.config.slug is None:
            results.append(match(target, host, EndpointModels(base_url=host)))
            continue
        credential = resolve_credential_id(target.config)
        key = (host, credential)
        if key not in listings:
            listings[key] = await _fetch(config, host, credential, read_key, client)
        results.append(match(target, host, listings[key]))
    return results


async def _fetch(
    config: ModelConfig,
    host: str,
    credential: str | None,
    read_key: KeyReader,
    client: httpx.AsyncClient | None,
) -> EndpointModels:
    api_key: str | None = None
    if credential is not None:
        refusal = key_refusal(config, credential, host)
        if refusal is not None:
            return EndpointModels(base_url=host, error=refusal)
        try:
            api_key = await read_key(credential)
        except (KeychainError, NotImplementedError) as e:
            return EndpointModels(base_url=host, error=str(e))
    return await list_endpoint_models(host, api_key, client=client)
