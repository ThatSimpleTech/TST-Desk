"""Resolve a tier's model slug from its endpoint (TD-1805).

The shipped ``local`` preset names an endpoint but no model tag.  A loopback
OpenAI-compatible endpoint is a convention — Ollama, vLLM, LM Studio and
llama.cpp all serve one — but a model tag is one developer's machine, and
shipping it as every user's default is the wrong default for a
bring-your-own-model product.  So a tier may leave ``slug`` unset, and the
model is read from ``GET /v1/models`` the first time a turn needs it.

Three rules keep that from becoming a surprise:

* **Config always wins.**  A slug present in ``config.yaml`` is never
  overridden and never triggers a request; discovery only fills a blank.
* **Loopback only.**  :class:`~tstd.config.TierConfig` refuses an unset slug
  on an off-box endpoint at validation time, so discovery is structurally
  unreachable for a remote tier — no request ever leaves this machine and
  no credential is ever needed (§2.2, §2.3).
* **Never guess.**  Exactly one served model resolves.  Zero or several
  raise :class:`~tstd.config.ModelDiscoveryError` naming the endpoint, what
  it served, and the one-line fix.  ``/v1/models`` lists embedding models
  next to chat models, so "take the first" would happily bind the agent to
  something that cannot answer a chat completion at all.

Nothing here writes anywhere: no key is read or sent, and a resolved slug
lives in the loaded config for the life of the process only.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import TIER_NAMES, ModelConfig, ModelDiscoveryError
from .logging import get_logger

log = get_logger("tstd.discovery")

DISCOVERY_TIMEOUT = 10.0
"""Seconds to wait on ``/v1/models``.  It lists what is installed rather
than loading anything, so it answers immediately or not at all."""

_START_SERVER_FIX = (
    "Start a local OpenAI-compatible model server at that address "
    "(Ollama, vLLM, LM Studio, llama.cpp), or point the tier's base_url at one."
)


def _model_ids(payload: Any) -> list[str]:
    """Model ids from an OpenAI ``/v1/models`` body, ignoring junk entries."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("data")
    if not isinstance(entries, list):
        return []
    return [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    ]


async def discover_model(
    base_url: str,
    *,
    tier: str = "",
    client: httpx.AsyncClient | None = None,
) -> str:
    """The single model *base_url* serves, for a tier that names none.

    Parameters
    ----------
    base_url:
        The OpenAI-compatible endpoint, e.g. ``http://127.0.0.1:11434/v1``.
    tier:
        Tier name, used only to make the fix text point at the right key.
    client:
        Optional pre-configured client, so a caller (or a test) supplies its
        own transport.  One is created and closed per call otherwise.

    Returns
    -------
    The model id to send as ``model`` on every request for this tier.

    Raises
    ------
    ModelDiscoveryError
        The endpoint is unreachable, answers something that is not an
        OpenAI model list, serves nothing, or serves several models — in
        which case picking one would be a silent guess.
    """
    where = f"the {tier} tier's " if tier else ""
    slug_fix = f"Name it in {where}`slug:` in config.yaml."
    endpoint = base_url.rstrip("/")

    owned = client is None
    http = client or httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT)
    try:
        try:
            # No Authorization header: the endpoint is on-box by
            # construction, so there is nothing to authenticate against and
            # no key to read (TD-1801).
            response = await http.get(
                f"{endpoint}/models",
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise ModelDiscoveryError(
                f"no model server answered at {endpoint}: {e}",
                endpoint=endpoint,
                fix=_START_SERVER_FIX,
            ) from e
    finally:
        if owned:
            await http.aclose()

    if response.status_code != 200:
        raise ModelDiscoveryError(
            f"{endpoint}/models returned HTTP {response.status_code}",
            endpoint=endpoint,
            fix=_START_SERVER_FIX,
        )

    try:
        ids = _model_ids(response.json())
    except ValueError as e:
        raise ModelDiscoveryError(
            f"{endpoint}/models did not return JSON: {e}",
            endpoint=endpoint,
            fix=_START_SERVER_FIX,
        ) from e

    if not ids:
        raise ModelDiscoveryError(
            f"{endpoint} is running but serves no models",
            endpoint=endpoint,
            fix="Load a model into that server, then send the message again.",
        )
    if len(ids) > 1:
        raise ModelDiscoveryError(
            f"{endpoint} serves {len(ids)} models and config names none of them: "
            f"{', '.join(sorted(ids))}",
            endpoint=endpoint,
            fix=slug_fix,
        )

    log.info(
        "model resolved from endpoint",
        extra={"extra_fields": {"endpoint": endpoint, "tier": tier, "model": ids[0]}},
    )
    return ids[0]


async def resolve_tier_slugs(
    config: ModelConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Fill in every unset slug of *config*'s active preset, in place.

    Idempotent and cheap: a preset whose slugs are all set costs a dict scan
    and makes no request, so callers may invoke it on every turn rather than
    tracking whether it has run.  The resolved value is held in memory only
    — it is never written back to ``config.yaml`` (§2.2, §2.7), so a model
    swapped on the server is picked up by the next process.

    Tiers sharing one endpoint are discovered once: the shipped ``local``
    preset points all three at the same server, and three identical requests
    would be noise.
    """
    tiers = config.tiers()
    unset = [name for name in TIER_NAMES if tiers[name].slug is None]
    if not unset:
        return

    seen: dict[str, str] = {}
    for name in unset:
        tier_cfg = tiers[name]
        resolved = seen.get(tier_cfg.base_url)
        if resolved is None:
            resolved = await discover_model(tier_cfg.base_url, tier=name, client=client)
            seen[tier_cfg.base_url] = resolved
        tier_cfg.slug = resolved
