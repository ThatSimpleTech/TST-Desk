"""Paired phones and the one-time pairing code (TD-5014).

A phone pairs once: the computer shows a code (and a QR of a
``tstdesk://pair`` link), the phone redeems it as its first frame, and
the daemon mints that phone its own key. After that the phone connects
with a normal ``hello`` carrying its key, so there is nothing to copy and
each device can be removed on its own.

Keys are stored as sha256 only, in ``{user_data_dir}/remote-devices.yaml``
(0600, written atomically). The code lives in daemon memory only: one at a
time, 10 minutes, single use, burned after 5 wrong tries. Neither a code
nor a key is ever logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import os
import re
import secrets
import socket
import tempfile
import time
import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO
from urllib.parse import quote

import segno
import yaml

from .logging import get_logger
from .protocol import PairingCode, RemoteDeviceSummary

log = get_logger("tstd.remote_devices")

REMOTE_DEVICES_FILE = "remote-devices.yaml"
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
CODE_LENGTH = 8
CODE_TTL_SECONDS = 600.0
MAX_FAILED_REDEEMS = 5
MAX_DEVICES = 32
LAST_SEEN_THROTTLE_SECONDS = 60.0
MAX_DEVICE_NAME = 64
UNNAMED_DEVICE = "Unnamed device"

PAIR_FAILED_MESSAGE = "That pairing code didn't work. Start pairing again on the computer."
PAIR_LOCKED_MESSAGE = "Too many wrong codes. Start pairing again on the computer."

Clock = Callable[[], float]
RedeemResult = Literal["ok", "failed", "locked"]

_FILE_VERSION = 1
_ID_RE = re.compile(r"^dev_[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Typed by hand from a screen, so the look-alikes map to the digits.
_CODE_FIXUPS = str.maketrans({"O": "0", "I": "1", "L": "1"})


class DeviceLimitError(ValueError):
    """Raised when a pairing would go past ``MAX_DEVICES``."""


# ── Small helpers ────────────────────────────────────────────────────


def normalize_code(raw: str) -> str | None:
    """The canonical 8-character code, or None when *raw* cannot be one.

    Uppercase, drop spaces and dashes, O->0, I->1, L->1. Every consumer
    (phone, CLI, daemon) applies the same rule before comparing.
    """
    cleaned = "".join(raw.split()).replace("-", "").upper().translate(_CODE_FIXUPS)
    if len(cleaned) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in cleaned):
        return None
    return cleaned


def display_code(code: str) -> str:
    """``ABCD2345`` -> ``ABCD-2345`` for a person to read and type."""
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


def sanitize_device_name(raw: str) -> str:
    """Trimmed, control characters removed, at most 64 characters."""
    kept = "".join(ch for ch in raw if unicodedata.category(ch) not in {"Cc", "Zl", "Zp"})
    name = kept.strip()[:MAX_DEVICE_NAME].rstrip()
    return name or UNNAMED_DEVICE


def desktop_name() -> str:
    """This computer's short host name, as the phone will show it."""
    return socket.gethostname().split(".")[0].strip() or "TST Desk"


def iso_utc(ts: float) -> str:
    """``2026-10-14T10:40:00Z`` — the one timestamp shape on this wire."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pairing_link(host: str, port: int, code: str, name: str) -> str:
    """The ``tstdesk://pair`` link the QR carries and the phone parses."""
    return f"tstdesk://pair?h={quote(host, safe=':')}&p={port}&c={code}&n={quote(name, safe='')}"


def _qr(link: str) -> segno.QRCode:
    return segno.make(link, error="m", boost_error=False)


def pairing_qr_svg(link: str) -> str:
    """A standalone black-on-white ``<svg>``: no XML declaration, no script.

    A viewBox and no fixed size, so the page scales it to its box.
    """
    buf = io.BytesIO()
    _qr(link).save(
        buf,
        kind="svg",
        xmldecl=False,
        border=2,
        dark="#000",
        light="#fff",
        omitsize=True,
        svgclass=None,
        lineclass=None,
        nl=False,
    )
    return buf.getvalue().decode("ascii")


def write_terminal_qr(link: str, out: TextIO) -> None:
    """The same QR in block characters for ``tst pair``."""
    _qr(link).terminal(out=out, border=2, compact=True)


def pairing_code_event(code: str, expires_at: float, host: str, port: int) -> PairingCode:
    """The ``start_pairing`` reply for the code the daemon just minted."""
    name = desktop_name()
    link = pairing_link(host, port, code, name)
    return PairingCode(
        code=code,
        display_code=display_code(code),
        host=host,
        port=port,
        desktop_name=name,
        link=link,
        qr_svg=pairing_qr_svg(link),
        expires_at=iso_utc(expires_at),
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── The pairing code ─────────────────────────────────────────────────


@dataclass
class _ActiveCode:
    code: str
    expires_at: float
    failures: int = 0
    burned: bool = False


class PairingCodes:
    """The one active pairing code, in memory only. A new start replaces it."""

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self._active: _ActiveCode | None = None

    def start(self) -> tuple[str, float]:
        """Mint a fresh code. Returns ``(code, expires_at unix seconds)``."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._active = _ActiveCode(code=code, expires_at=self._clock() + CODE_TTL_SECONDS)
        return code, self._active.expires_at

    def cancel(self) -> None:
        self._active = None

    @property
    def active(self) -> bool:
        """True while a code can still be redeemed."""
        found = self._active
        return found is not None and not found.burned and self._clock() < found.expires_at

    def redeem(self, raw: str) -> RedeemResult:
        """Spend the code. Wrong tries count; the fifth one burns it."""
        found = self._active
        if found is None:
            return "failed"
        if self._clock() >= found.expires_at:
            self._active = None
            return "failed"
        if found.burned:
            return "locked"
        candidate = normalize_code(raw) or ""
        if hmac.compare_digest(candidate.encode(), found.code.encode()):
            self._active = None
            return "ok"
        found.failures += 1
        if found.failures >= MAX_FAILED_REDEEMS:
            found.burned = True
            return "locked"
        return "failed"


# ── The device registry ──────────────────────────────────────────────


@dataclass
class _Device:
    id: str
    name: str
    token_sha256: str
    paired_at: str
    last_seen: str | None = None


def _timestamp(value: object) -> str | None:
    # A hand-edited file may hold an unquoted timestamp; YAML reads that
    # as a datetime, not a string.
    if isinstance(value, datetime):
        return iso_utc(value.timestamp())
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _device_from(raw: object) -> _Device | None:
    if not isinstance(raw, dict):
        return None
    device_id, name, digest = raw.get("id"), raw.get("name"), raw.get("token_sha256")
    if not isinstance(device_id, str) or not _ID_RE.match(device_id):
        return None
    if not isinstance(name, str) or not isinstance(digest, str) or not _SHA256_RE.match(digest):
        return None
    paired_at = _timestamp(raw.get("paired_at"))
    if paired_at is None:
        return None
    last_raw = raw.get("last_seen")
    last_seen = _timestamp(last_raw)
    if last_raw is not None and last_seen is None:
        return None
    return _Device(device_id, sanitize_device_name(name), digest, paired_at, last_seen)


def _entries(data: Any) -> list[Any] | None:
    """The device list, or None when the file is not ours to read."""
    if not isinstance(data, dict) or data.get("version") != _FILE_VERSION:
        return None
    devices = data.get("devices")
    if devices is None:
        return []
    return devices if isinstance(devices, list) else None


class RemoteDeviceRegistry:
    """Paired devices for this user data dir. Never holds a key, only its hash."""

    def __init__(self, data_dir: Path, clock: Clock = time.time) -> None:
        self._path = Path(data_dir) / REMOTE_DEVICES_FILE
        self._clock = clock
        self._devices: dict[str, _Device] = {}
        # Clock reading of the last last_seen write, per device.
        self._last_written: dict[str, float] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_full(self) -> bool:
        return len(self._devices) >= MAX_DEVICES

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def load(self) -> None:
        """Read the file. A damaged one is set aside, never a failed start."""
        self._devices = {}
        self._last_written = {}
        if not self._path.exists():
            return
        try:
            data: Any = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            data = None
        entries = _entries(data)
        if entries is None:
            self._set_aside()
            return
        skipped = 0
        for raw in entries:
            device = _device_from(raw)
            if device is None or device.id in self._devices:
                skipped += 1
                continue
            self._devices[device.id] = device
        if skipped:
            log.warning(
                "skipped malformed remote device entries",
                extra={"extra_fields": {"skipped": skipped, "path": str(self._path)}},
            )

    def _set_aside(self) -> None:
        target = self._path.with_name(f"{REMOTE_DEVICES_FILE}.corrupt-{int(self._clock())}")
        try:
            os.replace(self._path, target)
        except OSError:
            log.warning(
                "remote devices file unreadable and could not be set aside",
                extra={"extra_fields": {"path": str(self._path)}},
            )
            return
        log.warning(
            "remote devices file unreadable; set aside and starting empty",
            extra={"extra_fields": {"path": str(target)}},
        )

    def save(self) -> None:
        """Atomic temp + rename, chmod 0600."""
        payload = {
            "version": _FILE_VERSION,
            "devices": [
                {
                    "id": d.id,
                    "name": d.name,
                    "token_sha256": d.token_sha256,
                    "paired_at": d.paired_at,
                    "last_seen": d.last_seen,
                }
                for d in self._devices.values()
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".remote-devices.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def add(self, name: str, avoid: Collection[str] = ()) -> tuple[RemoteDeviceSummary, str]:
        """Pair a device. Returns its summary and its key — the only copy.

        *avoid* holds tokens the key must never equal (the loopback and
        legacy remote tokens).
        """
        if self.is_full:
            raise DeviceLimitError(f"already {MAX_DEVICES} paired devices")
        token = secrets.token_hex(32)
        while token in avoid or self.verify(token) is not None:
            token = secrets.token_hex(32)
        device_id = f"dev_{secrets.token_hex(6)}"
        while device_id in self._devices:
            device_id = f"dev_{secrets.token_hex(6)}"
        device = _Device(
            id=device_id,
            name=sanitize_device_name(name),
            token_sha256=_hash_token(token),
            paired_at=iso_utc(self._clock()),
        )
        self._devices[device_id] = device
        try:
            self.save()
        except OSError:
            # A key the file does not hold would stop working at restart.
            del self._devices[device_id]
            raise
        log.info("remote device paired", extra={"extra_fields": {"device_id": device_id}})
        return self._summary(device, connected=False), token

    def verify(self, token: str) -> str | None:
        """The device id whose key this is, or None. Checks every entry."""
        digest = _hash_token(token).encode()
        match: str | None = None
        for device in self._devices.values():
            if hmac.compare_digest(digest, device.token_sha256.encode()):
                match = device.id
        return match

    def remove(self, device_id: str) -> bool:
        """Forget a device. False when the id is unknown."""
        if self._devices.pop(device_id, None) is None:
            return False
        self._last_written.pop(device_id, None)
        self.save()
        log.info("remote device removed", extra={"extra_fields": {"device_id": device_id}})
        return True

    def touch_last_seen(self, device_id: str) -> bool:
        """Record that the device is here; writes at most once a minute.

        Returns True when the file was written. A failed write is logged
        and skipped: a full disk must not drop the phone's connection.
        """
        device = self._devices.get(device_id)
        if device is None:
            return False
        now = self._clock()
        last = self._last_written.get(device_id)
        if last is not None and now - last < LAST_SEEN_THROTTLE_SECONDS:
            return False
        previous = device.last_seen
        device.last_seen = iso_utc(now)
        try:
            self.save()
        except OSError:
            device.last_seen = previous
            log.warning(
                "could not record remote device last_seen",
                extra={"extra_fields": {"device_id": device_id}},
            )
            return False
        self._last_written[device_id] = now
        return True

    def summaries(self, connected: Collection[str] = ()) -> list[RemoteDeviceSummary]:
        """What the UI may see: never a key or its hash."""
        return [self._summary(d, connected=d.id in connected) for d in self._devices.values()]

    @staticmethod
    def _summary(device: _Device, *, connected: bool) -> RemoteDeviceSummary:
        return RemoteDeviceSummary(
            id=device.id,
            name=device.name,
            paired_at=device.paired_at,
            last_seen=device.last_seen,
            connected=connected,
        )
