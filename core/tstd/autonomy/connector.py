"""Select the configured judgment connector (TD-708).

The hosted connector is imported here, not in the agent loop, so the
loop does not name a vendor.  The worker tier is the default and needs
no credential.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..config import JudgmentsConfig
from ..keychain import KeychainError, get_api_key
from ..logging import get_logger
from .judgment import JudgmentBackend, WorkerChatJudgmentBackend
from .typesafe import TypeSafeJudgmentBackend
from .worker import legacy_classifier_renderer

log = get_logger("tstd.judgment.connector")


async def judgment_backend_for(
    cfg: JudgmentsConfig,
    worker_completion: Callable[[str], Awaitable[str]],
) -> JudgmentBackend:
    """The configured connector, or the worker tier when it cannot serve.

    A hosted connector needs its keychain key (prime §2.2).  A missing
    key logs and falls back to the worker connector — fail toward
    working, never a block.
    """
    if cfg.backend == "typesafe":
        if not cfg.typesafe_base_url.strip():
            log.warning(
                "judgments.backend is typesafe but no base_url is set; using the worker tier"
            )
        else:
            try:
                key = await get_api_key(cfg.typesafe_credential)
            except KeychainError:
                log.warning(
                    "judgments.backend is typesafe but no %r key is stored; using the worker tier",
                    cfg.typesafe_credential,
                )
            else:
                return TypeSafeJudgmentBackend(
                    base_url=cfg.typesafe_base_url,
                    api_key=key,
                    model=cfg.typesafe_model,
                )
    return WorkerChatJudgmentBackend(worker_completion)


async def classifier_connector(
    cfg: JudgmentsConfig,
    worker_completion: Callable[[str], Awaitable[str]],
) -> JudgmentBackend:
    """The connector the decision classifier calls.

    The default keeps the legacy classifier prompt.  A hosted connector
    is used only when it was actually built (a missing key falls back
    here, not just inside ``judgment_backend_for``, so the prompt bytes
    stay the legacy ones).
    """
    if cfg.backend != "worker":
        selected = await judgment_backend_for(cfg, worker_completion)
        host = getattr(selected, "remote_host", None)
        if isinstance(host, str) and host.strip():
            return selected
    return WorkerChatJudgmentBackend(worker_completion, renderer=legacy_classifier_renderer)
