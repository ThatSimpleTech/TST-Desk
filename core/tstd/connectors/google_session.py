"""Authorization-code coordinator for the daemon (TD-4854).

The verifier, the state, the authorization code, and the client secret
stay here. The window sees a phase and, once signed in, the mailbox
address. Tokens stay in the keychain. The loopback listener and PKCE
are the ones TD-4853 already binds to 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from ..config import GoogleConnectorConfig, ModelConfig
from ..keychain import KeychainError
from ..logging import get_logger
from ..protocol import GoogleConnectorState, GooglePhase, GoogleStatus
from .google_expiry import bind_expired, clear_expired, current_expired
from .google_flow import authorize_host_ok, authorize_url, exchange_code, fetch_account
from .google_models import (
    OFF_MESSAGE,
    SECRET_MESSAGE,
    STATE_MESSAGE,
    GoogleError,
    oauth_phase,
    public_oauth_message,
)
from .google_tokens import KeychainSecretStore, KeychainTokenStore, SecretStore, TokenStore
from .microsoft_loopback import LoopbackAuth
from .microsoft_models import Callback, MicrosoftError
from .microsoft_pkce import new_pkce, new_state, states_match

log = get_logger("tstd.connectors.google")

ConfigFn = Callable[[], ModelConfig]
BroadcastFn = Callable[[GoogleStatus], Awaitable[None]]
Opener = Callable[[str], object]

_LIVE = frozenset({"pending", "connected", "expired", "denied", "error"})
_PHASES = frozenset({"off", "idle", "pending", "connected", "denied", "error", "expired"})
_WAITING = "Waiting for Google sign-in in the browser."


class LoopbackLike(Protocol):
    bound_host: str

    async def start(self) -> str: ...

    async def wait(self, timeout_seconds: float) -> Callback: ...

    async def aclose(self) -> None: ...


def _status(state: GoogleConnectorState) -> GoogleStatus:
    return GoogleStatus(
        seq=1,
        enabled=state.enabled,
        connected=state.connected,
        phase=state.phase,
        account=state.account,
        message=state.message,
        client_secret_stored=state.client_secret_stored,
    )


def _phase(value: str) -> GooglePhase:
    if value in _PHASES:
        return cast(GooglePhase, value)
    return "error"


class GoogleCoordinator:
    """One sign-in at a time. A new connect cancels the previous wait."""

    def __init__(
        self,
        config_fn: ConfigFn,
        broadcast: BroadcastFn,
        *,
        store: TokenStore | None = None,
        secrets: SecretStore | None = None,
        transport: Any = None,
        opener: Opener | None = None,
        loopback_factory: Callable[[], LoopbackLike] | None = None,
    ) -> None:
        self._config_fn = config_fn
        self._broadcast = broadcast
        self._store = store if store is not None else KeychainTokenStore()
        self._secrets = secrets if secrets is not None else KeychainSecretStore()
        self._transport = transport
        self._opener = opener
        self._loopback_factory = loopback_factory or LoopbackAuth
        self._task: asyncio.Task[None] | None = None
        self._phase = "idle"
        self._account: str | None = None
        self._message = ""
        self._unbind = bind_expired(self._on_expired)

    async def aclose(self) -> None:
        self._unbind()
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

    async def connect(self) -> GoogleConnectorState:
        cfg = self._config_fn().connectors.google
        await self._cancel()
        if not cfg.enabled():
            self._clear(phase="off", message=OFF_MESSAGE)
            return await self.snapshot()
        if not await self._secrets.stored():
            self._clear(phase="error", message=SECRET_MESSAGE)
            return await self.snapshot()
        self._phase = "pending"
        self._account = None
        self._message = _WAITING
        self._task = asyncio.create_task(self._run(cfg))
        return await self.snapshot()

    async def disconnect(self) -> GoogleConnectorState:
        await self._cancel()
        clear_expired()
        try:
            await self._store.delete()
        except KeychainError:
            self._phase = "error"
            self._account = None
            self._message = "Could not delete the Google sign-in."
            return await self.snapshot()
        self._account = None
        cfg = self._config_fn().connectors.google
        if cfg.enabled():
            self._clear(phase="idle", message="")
        else:
            self._clear(phase="off", message=OFF_MESSAGE)
        return await self.snapshot()

    async def snapshot(self) -> GoogleConnectorState:
        cfg = self._config_fn().connectors.google
        if not cfg.enabled():
            return GoogleConnectorState(
                enabled=False, connected=False, phase="off", message=OFF_MESSAGE
            )
        stored = await self._store.stored()
        secret_stored = await self._secrets.stored()
        expired = current_expired()
        if expired and stored and self._phase not in _LIVE:
            self._phase = "expired"
            self._message = expired
        if self._phase in _LIVE:
            show_account = stored and self._phase != "pending"
            return GoogleConnectorState(
                enabled=True,
                connected=stored,
                phase=_phase(self._phase),
                account=self._account if show_account else None,
                message=self._message,
                client_secret_stored=secret_stored,
            )
        if stored:
            return GoogleConnectorState(
                enabled=True,
                connected=True,
                phase="connected",
                account=self._account,
                client_secret_stored=secret_stored,
            )
        return GoogleConnectorState(
            enabled=True, connected=False, phase="idle", client_secret_stored=secret_stored
        )

    async def _run(self, cfg: GoogleConnectorConfig) -> None:
        loopback = self._loopback_factory()
        try:
            try:
                await self._finish(cfg, loopback)
            except GoogleError as exc:
                self._phase = exc.phase
                self._message = exc.message
                log.warning("google sign-in failed")
            except MicrosoftError as exc:
                # The shared listener's text does not name a provider.
                self._phase = exc.phase
                self._message = exc.message
                log.warning("google sign-in failed")
            finally:
                # Cancellation must still close the port. shield keeps aclose
                # alive when the task itself is the thing being cancelled.
                with contextlib.suppress(Exception):
                    await asyncio.shield(loopback.aclose())
        except asyncio.CancelledError:
            raise
        await self._safe_broadcast(_status(await self.snapshot()))

    async def _finish(self, cfg: GoogleConnectorConfig, loopback: LoopbackLike) -> None:
        redirect = await loopback.start()
        if loopback.bound_host != "127.0.0.1":
            raise GoogleError("Sign-in could not listen on this machine.")
        try:
            verifier, challenge = new_pkce()
        except MicrosoftError:
            raise GoogleError("Google sign-in failed.") from None
        state = new_state()
        url = authorize_url(
            auth_host=cfg.auth_host,
            client_id=cfg.client_id,
            redirect_uri=redirect,
            state=state,
            code_challenge=challenge,
        )
        if not authorize_host_ok(url, cfg.auth_host):
            raise GoogleError("Google sign-in failed.")
        try:
            await self._open(url)
        except GoogleError:
            raise
        except Exception:
            raise GoogleError("Could not open a browser.") from None
        callback = await loopback.wait(cfg.sign_in_timeout_seconds)
        if callback.error:
            raise GoogleError(
                public_oauth_message(callback.error), phase=oauth_phase(callback.error)
            )
        if not states_match(state, callback.state):
            raise GoogleError(STATE_MESSAGE)
        if not callback.code:
            raise GoogleError("Google sign-in failed.")
        secret = await self._secrets.load()
        granted = await exchange_code(
            token_host=cfg.token_host,
            client_id=cfg.client_id,
            client_secret=secret,
            code=callback.code,
            redirect_uri=redirect,
            code_verifier=verifier,
            timeout_seconds=cfg.timeout_seconds,
            transport=self._transport,
        )
        await self._store.save(granted.tokens)
        clear_expired()
        account = await fetch_account(cfg, granted.tokens.access_token, transport=self._transport)
        self._account = account or granted.account
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

    async def _on_expired(self, message: str) -> None:
        """A refresh died. Settings should say reconnect, not stay connected."""
        self._phase = "expired"
        self._message = message
        await self._safe_broadcast(_status(await self.snapshot()))

    async def _safe_broadcast(self, event: GoogleStatus) -> None:
        try:
            await self._broadcast(event)
        except Exception:
            log.warning("google status broadcast failed")

    def _clear(self, *, phase: str, message: str) -> None:
        self._phase = phase
        self._message = message


async def state_from_config(config: ModelConfig) -> GoogleConnectorState:
    """setup_state when no sign-in is in progress in this process."""
    cfg = config.connectors.google
    if not cfg.enabled():
        return GoogleConnectorState(
            enabled=False, connected=False, phase="off", message=OFF_MESSAGE
        )
    store = KeychainTokenStore()
    secrets = KeychainSecretStore()
    secret_stored = await secrets.stored()
    if await store.stored():
        expired = current_expired()
        if expired:
            return GoogleConnectorState(
                enabled=True,
                connected=True,
                phase="expired",
                message=expired,
                client_secret_stored=secret_stored,
            )
        return GoogleConnectorState(
            enabled=True, connected=True, phase="connected", client_secret_stored=secret_stored
        )
    return GoogleConnectorState(
        enabled=True, connected=False, phase="idle", client_secret_stored=secret_stored
    )
