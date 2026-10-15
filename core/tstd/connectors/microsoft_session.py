"""Authorization-code coordinator for the daemon (TD-4853).

The verifier, the state, and the authorization code stay here. The
window sees a phase and, once signed in, the mailbox address. Tokens
stay in the keychain.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from ..config import MicrosoftConnectorConfig, ModelConfig
from ..keychain import KeychainError
from ..logging import get_logger
from ..protocol import MicrosoftConnectorState, MicrosoftPhase, MicrosoftStatus
from .microsoft_flow import authorize_host_ok, authorize_url, exchange_code, fetch_account
from .microsoft_loopback import LoopbackAuth
from .microsoft_models import (
    OFF_MESSAGE,
    STATE_MESSAGE,
    Callback,
    MicrosoftError,
    oauth_phase,
    public_oauth_message,
)
from .microsoft_pkce import new_pkce, new_state, states_match
from .microsoft_tokens import KeychainTokenStore, TokenStore

log = get_logger("tstd.connectors.microsoft")

ConfigFn = Callable[[], ModelConfig]
BroadcastFn = Callable[[MicrosoftStatus], Awaitable[None]]
Opener = Callable[[str], object]

_LIVE = frozenset({"pending", "connected", "expired", "denied", "error"})
_PHASES = frozenset({"off", "idle", "pending", "connected", "denied", "error", "expired"})
_WAITING = "Waiting for Microsoft sign-in in the browser."


class LoopbackLike(Protocol):
    bound_host: str

    async def start(self) -> str: ...

    async def wait(self, timeout_seconds: float) -> Callback: ...

    async def aclose(self) -> None: ...


def _status(state: MicrosoftConnectorState) -> MicrosoftStatus:
    return MicrosoftStatus(
        seq=1,
        enabled=state.enabled,
        connected=state.connected,
        phase=state.phase,
        account=state.account,
        message=state.message,
    )


def _phase(value: str) -> MicrosoftPhase:
    if value in _PHASES:
        return cast(MicrosoftPhase, value)
    return "error"


class MicrosoftCoordinator:
    """One sign-in at a time. A new connect cancels the previous wait."""

    def __init__(
        self,
        config_fn: ConfigFn,
        broadcast: BroadcastFn,
        *,
        store: TokenStore | None = None,
        transport: Any = None,
        opener: Opener | None = None,
        loopback_factory: Callable[[], LoopbackLike] | None = None,
    ) -> None:
        self._config_fn = config_fn
        self._broadcast = broadcast
        self._store = store if store is not None else KeychainTokenStore()
        self._transport = transport
        self._opener = opener
        self._loopback_factory = loopback_factory or LoopbackAuth
        self._task: asyncio.Task[None] | None = None
        self._phase = "idle"
        self._account: str | None = None
        self._message = ""

    async def aclose(self) -> None:
        await self._cancel()

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

    async def connect(self) -> MicrosoftConnectorState:
        cfg = self._config_fn().connectors.microsoft
        await self._cancel()
        if not cfg.enabled():
            self._clear(phase="off", message=OFF_MESSAGE)
            return await self.snapshot()
        self._phase = "pending"
        self._account = None
        self._message = _WAITING
        self._task = asyncio.create_task(self._run(cfg))
        return await self.snapshot()

    async def disconnect(self) -> MicrosoftConnectorState:
        await self._cancel()
        try:
            await self._store.delete()
        except KeychainError:
            self._phase = "error"
            self._account = None
            self._message = "Could not delete the Microsoft sign-in."
            return await self.snapshot()
        self._account = None
        cfg = self._config_fn().connectors.microsoft
        if cfg.enabled():
            self._clear(phase="idle", message="")
        else:
            self._clear(phase="off", message=OFF_MESSAGE)
        return await self.snapshot()

    async def snapshot(self) -> MicrosoftConnectorState:
        cfg = self._config_fn().connectors.microsoft
        if not cfg.enabled():
            return MicrosoftConnectorState(
                enabled=False, connected=False, phase="off", message=OFF_MESSAGE
            )
        stored = await self._store.stored()
        if self._phase in _LIVE:
            show_account = stored and self._phase != "pending"
            return MicrosoftConnectorState(
                enabled=True,
                connected=stored,
                phase=_phase(self._phase),
                account=self._account if show_account else None,
                message=self._message,
            )
        if stored:
            return MicrosoftConnectorState(
                enabled=True, connected=True, phase="connected", account=self._account
            )
        return MicrosoftConnectorState(enabled=True, connected=False, phase="idle")

    async def _run(self, cfg: MicrosoftConnectorConfig) -> None:
        loopback = self._loopback_factory()
        try:
            try:
                await self._finish(cfg, loopback)
            except MicrosoftError as exc:
                self._phase = exc.phase
                self._message = exc.message
                log.warning("microsoft sign-in failed")
            finally:
                # Cancellation must still close the port. shield keeps aclose
                # alive when the task itself is the thing being cancelled.
                with contextlib.suppress(Exception):
                    await asyncio.shield(loopback.aclose())
        except asyncio.CancelledError:
            raise
        await self._safe_broadcast(_status(await self.snapshot()))

    async def _finish(self, cfg: MicrosoftConnectorConfig, loopback: LoopbackLike) -> None:
        redirect = await loopback.start()
        if loopback.bound_host != "127.0.0.1":
            raise MicrosoftError("Sign-in could not listen on this machine.")
        verifier, challenge = new_pkce()
        state = new_state()
        url = authorize_url(
            login_host=cfg.login_host,
            tenant=cfg.tenant,
            client_id=cfg.client_id,
            redirect_uri=redirect,
            state=state,
            code_challenge=challenge,
        )
        if not authorize_host_ok(url, cfg.login_host):
            raise MicrosoftError("Microsoft sign-in failed.")
        try:
            await self._open(url)
        except MicrosoftError:
            raise
        except Exception:
            raise MicrosoftError("Could not open a browser.") from None
        callback = await loopback.wait(cfg.sign_in_timeout_seconds)
        if callback.error:
            raise MicrosoftError(
                public_oauth_message(callback.error), phase=oauth_phase(callback.error)
            )
        if not states_match(state, callback.state):
            raise MicrosoftError(STATE_MESSAGE)
        if not callback.code:
            raise MicrosoftError("Microsoft sign-in failed.")
        tokens = await exchange_code(
            login_host=cfg.login_host,
            tenant=cfg.tenant,
            client_id=cfg.client_id,
            code=callback.code,
            redirect_uri=redirect,
            code_verifier=verifier,
            timeout_seconds=cfg.timeout_seconds,
            transport=self._transport,
        )
        await self._store.save(tokens)
        self._account = await fetch_account(cfg, tokens.access_token, transport=self._transport)
        self._phase = "connected"
        self._message = ""

    async def _open(self, url: str) -> None:
        if self._opener is not None:
            self._opener(url)
            return
        import webbrowser

        await asyncio.to_thread(webbrowser.open, url)

    async def _cancel(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _safe_broadcast(self, event: MicrosoftStatus) -> None:
        try:
            await self._broadcast(event)
        except Exception:
            log.warning("microsoft status broadcast failed")

    def _clear(self, *, phase: str, message: str) -> None:
        self._phase = phase
        self._message = message


async def state_from_config(config: ModelConfig) -> MicrosoftConnectorState:
    """setup_state when no sign-in is in progress in this process."""
    cfg = config.connectors.microsoft
    if not cfg.enabled():
        return MicrosoftConnectorState(
            enabled=False, connected=False, phase="off", message=OFF_MESSAGE
        )
    store = KeychainTokenStore()
    if await store.stored():
        return MicrosoftConnectorState(enabled=True, connected=True, phase="connected")
    return MicrosoftConnectorState(enabled=True, connected=False, phase="idle")
