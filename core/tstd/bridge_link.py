"""One kept-up socket to a paired computer (TD-5015).

``UpstreamLink`` dials, says hello with the key and ``features:
["bridge"]``, and keeps the socket up: backoff 1, 2, 4 ... capped at 30 s
with jitter; 45 s of silence forces a reconnect (the other computer pings
every 15 s). The hub's control links and lanes both ride on it.

Status: ``connecting`` until the first handshake, ``unreachable`` once
60 s pass without one, ``connected`` while up. ``auth_failed`` or a 1008
"device removed" close is ``refused`` and stops retrying.
``version_unsupported`` is ``outdated`` and a locked keychain is
``locked``: both retry only on :meth:`reset_backoff` or every 5 minutes.
``devices_unavailable`` keeps retrying. Only Tailscale hosts are dialed,
checked before every dial (prime directive 3). Keys are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal

import websockets.exceptions
from websockets.asyncio.client import ClientConnection

from .keychain import KeychainError, KeychainLockedError
from .logging import get_logger
from .machine_pairing import (
    NOT_TAILSCALE_MESSAGE,
    OUTDATED_MESSAGE,
    DialPolicy,
    PairError,
    dial,
    hello_frame,
)

log = get_logger("tstd.bridge_link")

LinkStatus = Literal["connecting", "connected", "unreachable", "refused", "locked", "outdated"]

BACKOFF_FIRST_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
SILENCE_SECONDS = 45.0
UNREACHABLE_AFTER_SECONDS = 60.0
PARKED_RETRY_SECONDS = 300.0
HANDSHAKE_TIMEOUT_SECONDS = 10.0

REFUSED_MESSAGE = "{name} no longer knows this computer. Pair again or remove it."
LOCKED_MESSAGE = "The keychain is locked, so this computer can't use its key for {name}."
MISSING_KEY_MESSAGE = "This computer's key for {name} is missing. Pair again or remove it."

Sleep = Callable[[float], Coroutine[Any, Any, None]]


def backoff_delay(attempt: int, jitter: float) -> float:
    """Seconds before retry *attempt* (0-based): 1, 2, 4 ... at most 30.

    *jitter* in [0, 1) takes up to a fifth off, so many links never retry
    in step, and the cap holds.
    """
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_FIRST_SECONDS * (2.0 ** min(attempt, 16)))
    return base * (1.0 - 0.2 * jitter)


class UpstreamLink:
    """A socket to one paired computer that reconnects by itself."""

    def __init__(
        self,
        *,
        label: str,
        host: str,
        port: int,
        key: Callable[[], Awaitable[str]],
        policy: DialPolicy,
        on_open: Callable[[], Awaitable[None]],
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        on_status: Callable[[], None] | None = None,
        on_seen: Callable[[bool], None] | None = None,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        sleep: Sleep = asyncio.sleep,
        silence: float = SILENCE_SECONDS,
    ) -> None:
        self.label = label
        self.host = host
        self.port = port
        self.name = name or host
        self._key = key
        self._policy = policy
        self._on_open = on_open
        self._on_frame = on_frame
        self._on_status = on_status
        self._on_seen = on_seen
        self._clock = clock
        self._jitter = jitter
        self._sleep = sleep
        self._silence = silence
        self.status: LinkStatus = "connecting"
        self.detail: str | None = None
        self.remote_machine_id: str | None = None
        self.remote_name: str | None = None
        self.delays: list[float] = []
        self._attempt = 0
        self._streak_since: float | None = None
        self._ws: ClientConnection | None = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    # ── control ───────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._ws is not None and self.status == "connected"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name=f"link:{self.label}")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        task, self._task = self._task, None
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def reset_backoff(self) -> None:
        """Try again now (a viewer arrived, "Try now", a fresh pairing)."""
        self._attempt = 0
        self._wake.set()

    def send(self, frame: str) -> bool:
        """Queue one frame on the open socket. False (nothing queued) when closed."""
        if not self.is_open:
            return False
        self._outbox.put_nowait(frame)
        return True

    # ── the loop ──────────────────────────────────────────────────────

    def _set_status(self, status: LinkStatus, detail: str | None = None) -> None:
        if status == self.status and detail == self.detail:
            return
        self.status = status
        self.detail = detail
        if self._on_status is not None:
            self._on_status()

    def _down_status(self, detail: str | None = None) -> None:
        if self._streak_since is None:
            self._streak_since = self._clock()
        waited = self._clock() - self._streak_since
        status: LinkStatus = "unreachable" if waited >= UNREACHABLE_AFTER_SECONDS else "connecting"
        self._set_status(status, detail)

    async def _wait(self, seconds: float | None) -> None:
        """Sleep *seconds* (None: until woken), or until reset_backoff/stop."""
        self._wake.clear()
        waker = asyncio.create_task(self._wake.wait())
        sleeper = asyncio.create_task(self._sleep(seconds)) if seconds is not None else None
        pending = {waker} if sleeper is None else {waker, sleeper}
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _backoff(self) -> None:
        delay = backoff_delay(self._attempt, self._jitter())
        self._attempt += 1
        self.delays.append(delay)
        await self._wait(delay)

    async def _park(self, status: LinkStatus, detail: str, retry: float | None) -> None:
        """Stop retrying until reset_backoff (or *retry* seconds)."""
        self._set_status(status, detail)
        await self._wait(retry)
        self._attempt = 0

    async def _run(self) -> None:
        while not self._stopped:
            try:
                key = await self._key()
            except KeychainLockedError:
                await self._park(
                    "locked", LOCKED_MESSAGE.format(name=self.name), PARKED_RETRY_SECONDS
                )
                continue
            except KeychainError:
                await self._park("refused", MISSING_KEY_MESSAGE.format(name=self.name), None)
                continue
            outcome = await self._connect_once(key)
            del key
            if self._stopped:
                return
            if outcome == "refused":
                await self._park("refused", REFUSED_MESSAGE.format(name=self.name), None)
            elif outcome == "outdated":
                await self._park("outdated", OUTDATED_MESSAGE, PARKED_RETRY_SECONDS)
            elif outcome == "not_tailscale":
                await self._park("refused", NOT_TAILSCALE_MESSAGE, None)
            else:
                self._down_status(None if outcome == "retry" else outcome)
                await self._backoff()

    async def _connect_once(self, key: str) -> str:
        """One dial. Returns refused, outdated, not_tailscale, retry, or a detail."""
        try:
            ws = await dial(self.host, self.port, self._policy)
        except PairError:
            return "not_tailscale"
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
            return "retry"
        try:
            await ws.send(hello_frame(key))
            ack = await self._recv(ws, HANDSHAKE_TIMEOUT_SECONDS)
            if ack.get("type") == "error":
                code = ack.get("code")
                if code == "auth_failed":
                    return "refused"
                if code == "version_unsupported":
                    return "outdated"
                message = ack.get("message")
                return message if isinstance(message, str) and message else "retry"
            if ack.get("type") != "hello_ack":
                return "retry"
            machine_id, desktop = ack.get("machine_id"), ack.get("desktop_name")
            self.remote_machine_id = machine_id if isinstance(machine_id, str) else None
            self.remote_name = desktop if isinstance(desktop, str) and desktop else None
            return await self._serve(ws)
        except websockets.exceptions.ConnectionClosed as e:
            return "refused" if _removed(e) else "retry"
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException, ValueError):
            return "retry"
        finally:
            self._ws = None
            with contextlib.suppress(Exception):
                await ws.close()

    async def _serve(self, ws: ClientConnection) -> str:
        self._ws = ws
        self._attempt = 0
        self._streak_since = None
        self._outbox = asyncio.Queue()
        self._set_status("connected")
        if self._on_seen is not None:
            self._on_seen(False)
        log.info(
            "connected to a paired computer",
            extra={"extra_fields": {"link": self.label, "host": self.host, "port": self.port}},
        )
        writer = asyncio.create_task(self._write(ws), name=f"link-writer:{self.label}")
        try:
            await self._on_open()
            while True:
                frame = await self._recv(ws, self._silence)
                if self._on_seen is not None:
                    self._on_seen(False)
                try:
                    await self._on_frame(frame)
                except Exception:
                    # One bad frame must not take the link down.
                    log.exception(
                        "could not handle a frame from a paired computer",
                        extra={"extra_fields": {"link": self.label, "type": frame.get("type")}},
                    )
        except TimeoutError:
            log.info("paired computer went silent", extra={"extra_fields": {"link": self.label}})
            return "retry"
        except websockets.exceptions.ConnectionClosed as e:
            return "refused" if _removed(e) else "retry"
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            self._ws = None
            if self._on_seen is not None:
                self._on_seen(True)

    async def _write(self, ws: ClientConnection) -> None:
        while True:
            frame = await self._outbox.get()
            await ws.send(frame)

    async def _recv(self, ws: ClientConnection, within: float) -> dict[str, Any]:
        """The next JSON object frame. Application pings count as activity only."""
        while True:
            raw = await asyncio.wait_for(ws.recv(), within)
            if not isinstance(raw, str):
                continue
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "ping":
                if self._on_seen is not None:
                    self._on_seen(False)
                continue
            return frame


def _removed(error: websockets.exceptions.ConnectionClosed) -> bool:
    """Close 1008 "device removed" (or "auth_failed"): the other side forgot us."""
    rcvd = error.rcvd
    return (
        rcvd is not None and rcvd.code == 1008 and rcvd.reason in ("device removed", "auth_failed")
    )
