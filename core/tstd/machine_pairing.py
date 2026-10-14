"""Pairing this computer, as a hub, with another one (TD-5015).

The hub dials the other computer's remote listener and redeems a code
there, exactly as a phone does (TD-5014), but with ``kind: "machine"``.
It only ever dials a Tailscale host (100.64.0.0/10, fd7a:115c:a1e0::/48
or a ``*.ts.net`` name), checked at pair time and again before every
dial (prime directive 3). Codes and keys are never logged.

``DialPolicy`` is the one seam: production uses the Tailscale rule and
the root path; tests pass a rule that allows 127.0.0.1 and a path the
other daemon's test hook counts as remote.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets.exceptions
from websockets.asyncio.client import ClientConnection, connect

from .logging import get_logger
from .protocol import PROTOCOL_VERSION
from .remote_devices import normalize_code
from .tailscale_bind import is_tailscale_address

log = get_logger("tstd.machine_pairing")

MAX_FRAME_BYTES = 32 * 1024 * 1024
OPEN_TIMEOUT_SECONDS = 10.0
REDEEM_TIMEOUT_SECONDS = 10.0
REMOVE_TIMEOUT_SECONDS = 3.0
CLOSE_TIMEOUT_SECONDS = 2.0
BRIDGE_FEATURES = ("bridge",)

NOT_TAILSCALE_MESSAGE = (
    "That address isn't on Tailscale. Use the computer's Tailscale address or its .ts.net name."
)
UNREACHABLE_MESSAGE = (
    "Couldn't reach that computer. Check that TST Desk is open there with remote attach on."
)
OUTDATED_MESSAGE = "That computer runs a TST Desk this one can't talk to. Update both computers."


# One DNS label (RFC 1123). Anything else in a name (``/ ? # @ : %`` or
# a space) would let a host that merely ends in ``.ts.net`` point the
# socket URL at some other host.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def dial_allowed(host: str) -> bool:
    """The Tailscale rule: a Tailscale address, or a ``*.ts.net`` name."""
    cleaned = host.strip().strip("[]")
    if not cleaned or "%" in cleaned:
        # A zone id is never part of a Tailscale address, and Python
        # accepts any text after the "%".
        return False
    try:
        ipaddress.ip_address(cleaned)
    except ValueError:
        name = cleaned.lower().rstrip(".")
        labels = name.split(".")
        return (
            name.endswith(".ts.net")
            and len(labels) >= 3
            and all(_DNS_LABEL_RE.match(label) for label in labels)
        )
    return is_tailscale_address(cleaned)


@dataclass(frozen=True)
class DialPolicy:
    """Which hosts the hub may dial, and the path it dials."""

    allowed: Callable[[str], bool] = field(default=dial_allowed)
    path: str = ""


DEFAULT_POLICY = DialPolicy()


def ws_url(host: str, port: int, path: str = "") -> str:
    """The socket URL for *host* and *port*, with an IPv6 host in brackets."""
    bare = host.strip().strip("[]")
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        shown = bare
    else:
        shown = f"[{bare}]" if literal.version == 6 else bare
    return f"ws://{shown}:{port}{path}"


class PairError(Exception):
    """A typed pairing failure. The message is safe to show and to log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PairTarget:
    host: str
    port: int
    code: str
    name: str = ""


@dataclass(frozen=True)
class PairOutcome:
    """What ``pair_ok`` said. ``token`` is the key: memory and keychain only."""

    device_id: str
    device_name: str
    token: str = field(repr=False)
    desktop_name: str
    machine_id: str | None


def parse_target(
    *,
    link: str | None = None,
    host: str | None = None,
    port: int | None = None,
    code: str | None = None,
    policy: DialPolicy = DEFAULT_POLICY,
) -> PairTarget:
    """A target from a ``tstdesk://pair`` link, or from address plus code.

    Raises PairError: ``pair_failed`` for a malformed link or code,
    ``not_tailscale`` for a host the policy will not dial.
    """
    name = ""
    if link is not None and link.strip():
        parts = urlsplit(link.strip())
        if parts.scheme != "tstdesk" or parts.netloc != "pair":
            raise PairError("pair_failed", "That isn't a TST Desk pairing link.")
        query = parse_qs(parts.query)
        host = (query.get("h") or [""])[0]
        raw_port = (query.get("p") or [""])[0]
        code = (query.get("c") or [""])[0]
        name = (query.get("n") or [""])[0]
        try:
            port = int(raw_port)
        except ValueError:
            raise PairError("pair_failed", "That pairing link has no port.") from None
    if not host or not host.strip():
        raise PairError("pair_failed", "Enter the other computer's address.")
    if port is None or not 1 <= port <= 65535:
        raise PairError("pair_failed", "Enter the other computer's port.")
    normalized = normalize_code(code or "")
    if normalized is None:
        raise PairError("pair_failed", "That pairing code doesn't look right. It has 8 characters.")
    cleaned = host.strip().strip("[]")
    if not policy.allowed(cleaned):
        raise PairError("not_tailscale", NOT_TAILSCALE_MESSAGE)
    return PairTarget(host=cleaned, port=port, code=normalized, name=name)


def hello_frame(key: str) -> str:
    """The hub's hello on another computer. Carries the key: never log it."""
    return json.dumps(
        {
            "type": "hello",
            "token": key,
            "version": PROTOCOL_VERSION,
            "features": list(BRIDGE_FEATURES),
        }
    )


def checked_url(host: str, port: int, path: str = "") -> str:
    """:func:`ws_url`, refused unless it dials exactly *host* and *port*.

    The backstop to the rule: whatever a host string holds, the URL the
    socket opens must name that same host and port, nothing else.
    """
    url = ws_url(host, port, path)
    try:
        parts = urlsplit(url)
        same = parts.hostname == host.strip().strip("[]").lower() and parts.port == port
    except ValueError:
        same = False
    if not same:
        raise PairError("not_tailscale", NOT_TAILSCALE_MESSAGE)
    return url


async def dial(host: str, port: int, policy: DialPolicy) -> ClientConnection:
    """Open a socket to a paired computer. The rule is checked here, every time."""
    if not policy.allowed(host):
        raise PairError("not_tailscale", NOT_TAILSCALE_MESSAGE)
    return await connect(
        checked_url(host, port, policy.path),
        max_size=MAX_FRAME_BYTES,
        open_timeout=OPEN_TIMEOUT_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
        ping_interval=None,
    )


async def _recv_json(ws: ClientConnection, within: float) -> dict[str, Any]:
    """The next frame that is not an application ping."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), within)
        if not isinstance(raw, str):
            continue
        frame = json.loads(raw)
        if isinstance(frame, dict) and frame.get("type") != "ping":
            return frame


def _pair_error(frame: dict[str, Any]) -> PairError:
    code = frame.get("code")
    message = frame.get("message")
    shown = message if isinstance(message, str) and message else "Pairing didn't work."
    if code in ("pair_failed", "pair_locked"):
        return PairError(str(code), shown)
    if code == "version_unsupported":
        return PairError("pair_failed", OUTDATED_MESSAGE)
    return PairError("pair_failed", shown)


async def redeem(
    target: PairTarget,
    *,
    device_name: str,
    replaces: str | None = None,
    policy: DialPolicy = DEFAULT_POLICY,
    wait_seconds: float = REDEEM_TIMEOUT_SECONDS,
) -> PairOutcome:
    """Spend the code on the other computer. Raises PairError.

    The pair frame says ``kind: "machine"``. ``replaces`` is the hub's
    current key there, sent only when re-pairing the same host and port.
    """
    frame: dict[str, Any] = {
        "type": "pair",
        "code": target.code,
        "device_name": device_name,
        "version": PROTOCOL_VERSION,
        "kind": "machine",
    }
    if replaces is not None:
        frame["replaces"] = replaces
    try:
        async with asyncio.timeout(wait_seconds):
            ws = await dial(target.host, target.port, policy)
            try:
                await ws.send(json.dumps(frame))
                reply = await _recv_json(ws, wait_seconds)
            finally:
                await ws.close()
    except PairError:
        raise
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException, ValueError):
        log.info(
            "could not reach a computer to pair",
            extra={"extra_fields": {"host": target.host, "port": target.port}},
        )
        raise PairError("machine_unreachable", UNREACHABLE_MESSAGE) from None
    if reply.get("type") == "error":
        raise _pair_error(reply)
    if reply.get("type") != "pair_ok":
        raise PairError("pair_failed", "That computer answered, but not with a pairing.")
    token, device_id = reply.get("token"), reply.get("device_id")
    if not isinstance(token, str) or not token or not isinstance(device_id, str):
        raise PairError("pair_failed", "That computer's answer was incomplete.")
    machine_id = reply.get("machine_id")
    desktop = reply.get("desktop_name")
    return PairOutcome(
        device_id=device_id,
        device_name=str(reply.get("device_name") or ""),
        token=token,
        desktop_name=desktop if isinstance(desktop, str) and desktop else target.name,
        machine_id=machine_id if isinstance(machine_id, str) else None,
    )


async def remove_self(
    host: str,
    port: int,
    key: str,
    device_id: str,
    *,
    policy: DialPolicy = DEFAULT_POLICY,
    wait_seconds: float = REMOVE_TIMEOUT_SECONDS,
) -> bool:
    """Best effort: remove this hub from the other computer's paired devices.

    Hello with the key, then ``remove_remote_device`` naming the hub's
    own device id (the gate the phone's "Forget this computer" uses).
    True when the other computer acknowledged it. Never raises.
    """
    try:
        async with asyncio.timeout(wait_seconds):
            ws = await dial(host, port, policy)
            try:
                await ws.send(hello_frame(key))
                ack = await _recv_json(ws, wait_seconds)
                if ack.get("type") != "hello_ack":
                    return False
                await ws.send(json.dumps({"type": "remove_remote_device", "device_id": device_id}))
                with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                    reply = await _recv_json(ws, wait_seconds)
                    return reply.get("type") == "setup_state"
                # Closed before the ack: "device removed" is still a yes.
                return ws.close_code == 1008
            finally:
                await ws.close()
    except (PairError, TimeoutError, OSError, websockets.exceptions.WebSocketException, ValueError):
        log.info(
            "could not remove this computer from another one",
            extra={"extra_fields": {"host": host, "port": port}},
        )
        return False
