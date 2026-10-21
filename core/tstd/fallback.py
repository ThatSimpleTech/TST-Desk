"""Backup endpoints per tier (TD-5120).

A tier can list ``fallbacks``: other models, on other endpoints, to send
the turn's request to when the tier's own endpoint is down. A llama.cpp
restart or a GPU swap used to fail the turn: every retry went back to the
endpoint that had just refused it.

The provider spends ``provider_retry`` on a failing call first. Only then
does the loop look here, and only for a failure another endpoint can help
with (``backup_wanted``):

- before any output, every failure the provider retries: a refused or
  dropped connection, a 429, a 500/502/503/504, a timeout, an empty
  stream or a stall;
- partway through the reply, the same failures of the endpoint: a
  dropped connection or an in-band 429 or 5xx (``stream_interrupted``
  with ``endpoint_failed``), and a stall (``stream_stalled``) once
  TD-5159's one withdraw-and-ask-again has stalled too. What the failed
  call showed is withdrawn first, so the window shows one reply. The
  provider never resends these to the same endpoint (TD-5152); asking
  another one is what the backup is for.

A stream that merely ends partway (no finish, no ``[DONE]``), an in-band
4xx and ``total_timeout`` stay on the tier and fail the turn as before.

The backup in use lasts for the rest of the user's turn, so a dead
endpoint costs the retry budget once per turn rather than once per model
call. The next turn starts on the tier's own model again: a backup is a
stand-in, never a silent new default, and it may cost money the tier's
own model does not.

Only the turn's own model call moves. The tool-call classifier, DoD polls
and the other off-turn calls stay on the tier's own model, so a safety
verdict never comes from a model the user did not pick for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import ModelConfig, TierConfig, apply_credential_host
from .local_worker import display_host
from .logging import get_logger
from .protocol import AssistantRetract, ModelFallback
from .reply_retry import StreamResult
from .router import TierName

if TYPE_CHECKING:
    from .session import Session

log = get_logger("tstd.fallback")

# A stall partway through a reply is a server that went quiet; it moves
# to a backup once TD-5159's retry has stalled too. A reply cut off by the
# endpoint carries ``endpoint_failed`` instead.
MID_REPLY_CODES = frozenset({"stream_stalled"})


def backup_wanted(result: StreamResult) -> bool:
    """True when *result* is a failure the next backup should get.

    *result* is what the turn's call returned after ``provider_retry``
    (and TD-5159's withdraw-and-retry) were spent. Before any output, the
    provider's own retryable flag is the test: it marks exactly the
    failures of the endpoint rather than of the request (an auth failure,
    a context overflow or a template error fails on any endpoint, or
    needs the user). After output the provider retries nothing, so its
    ``endpoint_failed`` flag marks the same failures. Stop and cancel are
    never either.
    """
    if not result.failed:
        return False
    if result.began:
        return result.error_code in MID_REPLY_CODES or result.endpoint_failed
    return result.retryable


def _identity(tier: TierConfig) -> tuple[str, str | None, str | None]:
    return (tier.base_url, tier.slug, tier.credential)


@dataclass
class BackupChain:
    """One tier's own model, then its backups in order.

    ``position`` 0 is the tier's own model; ``n`` is ``fallbacks[n - 1]``.
    Each entry has its credential host applied, as the tier does.
    """

    tier: TierName
    models: list[TierConfig]
    position: int = 0

    @property
    def current(self) -> TierConfig:
        return self.models[self.position]

    def advance(self) -> TierConfig | None:
        """Move to the next backup, or ``None`` when none is left."""
        if self.position + 1 >= len(self.models):
            return None
        self.position += 1
        return self.current


@dataclass
class TurnBackups:
    """Which tiers moved to a backup during one user turn.

    The loop makes a fresh one for every user turn, which is what sends
    the next turn back to each tier's own model.
    """

    chains: dict[TierName, BackupChain] = field(default_factory=dict)

    def chain_for(self, config: ModelConfig, tier: TierName, own: TierConfig) -> BackupChain:
        """The chain for *tier* whose own model is *own*.

        A chain built for a different own model (a computer-use session's
        worker remapped to the local preset, TD-3903) is replaced.
        """
        chain = self.chains.get(tier)
        if chain is not None and _identity(chain.models[0]) == _identity(own):
            return chain
        backups = [apply_credential_host(config, backup) for backup in own.fallbacks]
        chain = BackupChain(tier, [own, *backups])
        self.chains[tier] = chain
        return chain

    def peek(self, tier: TierName, own: TierConfig) -> TierConfig:
        """The model *tier* would call now, changing nothing.

        Its chain's current model when the chain was built for *own*, else
        *own* itself (a chain not built yet starts there).
        """
        chain = self.chains.get(tier)
        if chain is not None and _identity(chain.models[0]) == _identity(own):
            return chain.current
        return own

    def positions(self) -> dict[str, int]:
        """Tier name → backup position, for the tiers on a backup."""
        return {tier: chain.position for tier, chain in self.chains.items() if chain.position}

    def overlay(
        self, slugs: dict[str, str], hosts: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """*slugs* and *hosts* with each tier on a backup naming the backup."""
        slugs, hosts = dict(slugs), dict(hosts)
        for tier, chain in self.chains.items():
            if not chain.position:
                continue
            backup = chain.current
            if backup.slug is not None:
                slugs[tier] = backup.slug
            host = display_host(backup.base_url)
            if host:
                hosts[tier] = host
            else:
                hosts.pop(tier, None)
        return slugs, hosts


async def announce_switch(
    session: Session,
    chain: BackupChain,
    previous: TierConfig,
    failed: StreamResult,
) -> None:
    """Withdraw what the failed call showed, then say where the turn went.

    Called after ``chain`` has advanced, so ``chain.current`` is the
    backup about to be asked.
    """
    reason = failed.error_code or "provider_error"
    if failed.began and failed.response_id is not None:
        await session.event_log.add(
            AssistantRetract(
                session_id=session.id,
                response_id=failed.response_id,
                reason=reason,
                seq=1,
            )
        )
    backup = chain.current
    await session.event_log.add(
        ModelFallback(
            session_id=session.id,
            tier=chain.tier,
            from_model=previous.slug or "",
            from_host=display_host(previous.base_url),
            to_model=backup.slug or "",
            to_host=display_host(backup.base_url),
            fallback=chain.position,
            reason=reason,
            seq=1,
        )
    )
    log.warning(
        "tier endpoint failed, moving to a backup",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "tier": chain.tier,
                "code": reason,
                "from_model": previous.slug,
                "from_host": display_host(previous.base_url),
                "to_model": backup.slug,
                "to_host": display_host(backup.base_url),
                "fallback": chain.position,
            }
        },
    )
