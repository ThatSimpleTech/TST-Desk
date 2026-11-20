"""Whether each model on the picker can run right now (TD-5240).

A switch used to succeed and the next message then fail: the local server
was not started, the key was never stored, the Grok CLI was not installed.
This says so on the picker's row, before the click.

Three checks, none of which sends a key or leaves the machine:

* a role bound to a key: is that key in the keychain (presence only);
* a role served on this computer: does anything answer there;
* an entry on the Grok engine: is the Grok CLI found.

A provider's server is not asked: that would be a request to a third party
the person did not make, and a key's presence is what can be known without
one. The first problem found is the entry's status, in the order a person
would fix them: the engine, then a key, then a server.

Pure but for the three callables it is handed, so every case is a table row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from .config import ModelConfig, is_loopback_url, resolve_base_url, resolve_credential_id
from .protocol import PresetReady

KeyStored = Callable[[str], Awaitable[bool]]
Answers = Callable[[list[str]], Awaitable[Mapping[str, bool]]]


def _where(url: str) -> str:
    """``host:port`` of *url*, for a message. Never the path or a password."""
    try:
        parts = urlsplit(url)
        return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)
    except ValueError:
        return "its server"


def loopback_urls(config: ModelConfig) -> list[str]:
    """Every distinct URL on this computer that some entry's role is served at."""
    found: list[str] = []
    for preset in config.presets.values():
        for tier in preset.tier_map().values():
            url = resolve_base_url(config, tier).strip().rstrip("/")
            if is_loopback_url(url) and url not in found:
                found.append(url)
    return found


async def check_presets(
    config: ModelConfig, *, key_stored: KeyStored, answers: Answers, grok_found: bool
) -> list[PresetReady]:
    """One row per catalog entry, in catalog order."""
    up = await answers(loopback_urls(config))
    rows: list[PresetReady] = []
    for name in config.presets:
        rows.append(await _check(config, name, key_stored, up, grok_found))
    return rows


async def _check(
    config: ModelConfig,
    name: str,
    key_stored: KeyStored,
    up: Mapping[str, bool],
    grok_found: bool,
) -> PresetReady:
    preset = config.presets[name]
    if config.preset_engine(name) == "grok":
        if not grok_found:
            return PresetReady(
                preset=name,
                status="no_grok",
                detail="The Grok CLI was not found on this computer.",
            )
        # The Grok CLI brings its own model and sign-in: nothing else to check.
        return PresetReady(preset=name, status="ready")
    for tier in preset.tier_map().values():
        credential = resolve_credential_id(tier)
        if credential is not None and not await key_stored(credential):
            shown = config.credentials[credential].name if credential in config.credentials else ""
            return PresetReady(
                preset=name,
                status="no_key",
                detail=f"No key is stored for {shown or credential}.",
            )
    for tier in preset.tier_map().values():
        url = resolve_base_url(config, tier).strip().rstrip("/")
        if is_loopback_url(url) and not up.get(url, True):
            return PresetReady(
                preset=name,
                status="server_down",
                detail=f"Nothing is answering at {_where(url)}. Start the model server.",
            )
    return PresetReady(preset=name, status="ready")
