"""Device-flow coordinator for the daemon.

``device_code`` stays here. The window sees the user code and the
verification URL, then a later ``github_status`` when the poll finishes.
Login is remembered in this process only (TD-4852).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..config import ModelConfig
from ..keychain import KeychainError
from ..logging import get_logger
from ..protocol import GithubConnectorState, GithubPhase, GithubStatus
from .github_api import fetch_login
from .github_flow import poll_for_token, start_device_flow
from .github_models import OFF_MESSAGE, DeviceCode, GithubError, GithubTokens
from .github_tokens import KeychainTokenStore, TokenStore

log = get_logger("tstd.connectors.github")

ConfigFn = Callable[[], ModelConfig]
BroadcastFn = Callable[[GithubStatus], Awaitable[None]]

_LIVE = frozenset({"pending", "connected", "expired", "denied", "error"})


def _status(state: GithubConnectorState) -> GithubStatus:
    return GithubStatus(
        seq=1,
        enabled=state.enabled,
        connected=state.connected,
        phase=state.phase,
        user_code=state.user_code,
        verification_uri=state.verification_uri,
        login=state.login,
        message=state.message,
    )


class GithubCoordinator:
    """One device flow at a time. A new connect cancels the previous poll."""

    def __init__(
        self,
        config_fn: ConfigFn,
        broadcast: BroadcastFn,
        *,
        store: TokenStore | None = None,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        mono: Callable[[], float] | None = None,
        wall: Callable[[], float] | None = None,
    ) -> None:
        self._config_fn = config_fn
        self._broadcast = broadcast
        self._store = store if store is not None else KeychainTokenStore()
        self._transport = transport
        self._sleep = sleep
        self._mono = mono
        self._wall = wall
        self._task: asyncio.Task[None] | None = None
        self._phase = "idle"
        self._user_code = ""
        self._verification_uri = ""
        self._login: str | None = None
        self._message = ""

    async def aclose(self) -> None:
        await self._cancel_poll()

    async def connect_json(self) -> str:
        state = await self.connect()
        event = _status(state)
        await self._safe_broadcast(event)
        return event.model_dump_json()

    async def disconnect_json(self) -> str:
        state = await self.disconnect()
        event = _status(state)
        await self._safe_broadcast(event)
        return event.model_dump_json()

    async def connect(self) -> GithubConnectorState:
        cfg = self._config_fn().connectors.github
        await self._cancel_poll()
        if not cfg.enabled():
            self._clear(phase="off", message=OFF_MESSAGE)
            return await self.snapshot()
        try:
            code = await start_device_flow(
                client_id=cfg.client_id,
                web_host=cfg.web_host,
                timeout_seconds=cfg.timeout_seconds,
                transport=self._transport,
            )
        except GithubError as exc:
            self._clear(phase=exc.phase, message=exc.message)
            return await self.snapshot()
        self._phase = "pending"
        self._user_code = code.user_code
        self._verification_uri = code.verification_uri
        self._message = ""
        self._task = asyncio.create_task(
            self._poll(cfg.client_id, cfg.web_host, cfg.timeout_seconds, code)
        )
        return await self.snapshot()

    async def disconnect(self) -> GithubConnectorState:
        await self._cancel_poll()
        try:
            await self._store.delete()
        except KeychainError:
            self._phase = "error"
            self._user_code = ""
            self._verification_uri = ""
            self._message = "Could not delete the GitHub sign-in."
            return await self.snapshot()
        self._login = None
        cfg = self._config_fn().connectors.github
        if cfg.enabled():
            self._clear(phase="idle", message="")
        else:
            self._clear(phase="off", message=OFF_MESSAGE)
        return await self.snapshot()

    async def snapshot(self) -> GithubConnectorState:
        cfg = self._config_fn().connectors.github
        if not cfg.enabled():
            return GithubConnectorState(
                enabled=False, connected=False, phase="off", message=OFF_MESSAGE
            )
        stored = await self._store.stored()
        if self._phase in _LIVE:
            show_code = self._phase == "pending"
            phase = cast(GithubPhase, self._phase)
            return GithubConnectorState(
                enabled=True,
                connected=stored,
                phase=phase,
                user_code=self._user_code if show_code else "",
                verification_uri=self._verification_uri if show_code else "",
                login=self._login if stored else None,
                message=self._message,
            )
        if stored:
            return GithubConnectorState(
                enabled=True, connected=True, phase="connected", login=self._login
            )
        return GithubConnectorState(enabled=True, connected=False, phase="idle")

    async def _poll(
        self, client_id: str, web_host: str, timeout_seconds: float, code: DeviceCode
    ) -> None:
        try:
            tokens = await poll_for_token(
                client_id=client_id,
                web_host=web_host,
                device_code=code.device_code,
                interval=code.interval,
                expires_in=code.expires_in,
                timeout_seconds=timeout_seconds,
                transport=self._transport,
                sleep=self._sleep,
                mono=self._mono,
                wall=self._wall,
            )
            await self._store.save(tokens)
            self._login = await self._lookup_login(tokens)
            self._phase = "connected"
            self._user_code = ""
            self._verification_uri = ""
            self._message = ""
        except asyncio.CancelledError:
            raise
        except GithubError as exc:
            self._phase = exc.phase
            self._user_code = ""
            self._verification_uri = ""
            self._message = exc.message
            log.warning("github device flow failed")
        await self._safe_broadcast(_status(await self.snapshot()))

    async def _lookup_login(self, tokens: GithubTokens) -> str | None:
        cfg = self._config_fn().connectors.github
        return await fetch_login(cfg, tokens.access_token, transport=self._transport)

    async def _cancel_poll(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _safe_broadcast(self, event: GithubStatus) -> None:
        try:
            await self._broadcast(event)
        except Exception:
            log.warning("github status broadcast failed")

    def _clear(self, *, phase: str, message: str) -> None:
        self._phase = phase
        self._user_code = ""
        self._verification_uri = ""
        self._message = message


async def state_from_config(config: ModelConfig) -> GithubConnectorState:
    """setup_state when no device flow is in progress in this process."""
    cfg = config.connectors.github
    if not cfg.enabled():
        return GithubConnectorState(
            enabled=False, connected=False, phase="off", message=OFF_MESSAGE
        )
    store = KeychainTokenStore()
    if await store.stored():
        return GithubConnectorState(enabled=True, connected=True, phase="connected")
    return GithubConnectorState(enabled=True, connected=False, phase="idle")
