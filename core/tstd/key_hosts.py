"""A saved key goes only to its own server (TD-5182).

A turn already sends a key only where config says: the host saved with the
key (TD-1718), or, for a key saved without one, the URL of the model it is
bound to. Three messages let a client name a saved key by id instead, and
they did not follow that rule. ``list_endpoint_models`` sent the key to
whatever URL came with it. The Settings key test fell back to the main
model's server for a key no model uses. Neither checked the id, so a client
could name any keychain item that shares the ``tst-`` prefix, such as a
webhook URL or a sign-in token, and have it sent as a bearer token.

This module is the one statement of the rule. The daemon calls it before it
reads a key a client named.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from .config import (
    CREDENTIAL_ID_RE,
    RESERVED_CREDENTIAL_IDS,
    TIER_NAMES,
    ModelConfig,
    TierConfig,
    credential_base_url,
    is_loopback_url,
    resolve_base_url,
    resolve_credential_id,
)

Origin = tuple[str, str, int]


def origin(url: str) -> Origin | None:
    """``(scheme, host, port)`` for an http(s) URL, else None."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def is_key_id(credential_id: str) -> bool:
    """True for an id that can name an API key: a slug, not another account's."""
    return bool(CREDENTIAL_ID_RE.match(credential_id)) and (
        credential_id not in RESERVED_CREDENTIAL_IDS
    )


def unknown_key(config: ModelConfig, credential_id: str) -> str | None:
    """Why *credential_id* is not a saved key this client may use, or None."""
    if is_key_id(credential_id) and credential_id in config.credentials:
        return None
    return f"There is no saved key called {credential_id!r}."


def every_tier(config: ModelConfig) -> Iterator[TierConfig]:
    """Every tier of every preset, then each one's backups (TD-5120)."""
    for preset in config.presets.values():
        for name in TIER_NAMES:
            tier: TierConfig = getattr(preset, name)
            yield tier
            yield from tier.fallbacks


def key_hosts(config: ModelConfig, credential_id: str) -> list[str]:
    """Base URLs this key is already sent to.

    Its own host when it has one. Otherwise every model or backup URL that
    sends it: a key saved without a host leaves the URL to the model.
    """
    own = credential_base_url(config, credential_id)
    if own:
        return [own]
    urls: list[str] = []
    for tier in every_tier(config):
        if resolve_credential_id(tier) == credential_id:
            url = resolve_base_url(config, tier)
            if url not in urls:
                urls.append(url)
    return urls


def key_refusal(config: ModelConfig, credential_id: str, base_url: str) -> str | None:
    """Why the saved key *credential_id* must not go to *base_url*, or None."""
    unknown = unknown_key(config, credential_id)
    if unknown is not None:
        return unknown
    target = origin(base_url)
    hosts = key_hosts(config, credential_id)
    if target is not None and any(origin(url) == target for url in hosts):
        return None
    own = credential_base_url(config, credential_id)
    if own is None and is_loopback_url(base_url):
        # A key saved without a host is for a server on this computer.
        return None
    where = ", ".join(url.rstrip("/") for url in hosts) if hosts else "a server on this computer"
    return (
        f"That saved key is for {where}, so it is not sent anywhere else. "
        "Type the key for this server instead."
    )
