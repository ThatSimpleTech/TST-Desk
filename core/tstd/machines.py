"""Paired computers, as this hub knows them (TD-5015).

``{user_data_dir}/computers.yaml`` (0600, temp file then rename) is the
index; it never holds a key. Each computer's key is in the OS keychain
under account ``tst-machine.<mch>`` (service com.thatsimpletech.tstdesk;
see :func:`machine_key_account`, which cannot collide with a credential's
``tst-<id>`` because credential ids allow neither ``.`` nor ``_``),
and the keychain has no list API, so this file is how the hub knows
which accounts exist.

``id`` (``mch_`` + 12 hex) is this hub's own handle: the suffix of every
bridged session id and the keychain account. ``machine_id`` is the other
computer's identity (``mid_``), so re-pairing the same computer at a new
address keeps the same ``mch`` and every bridged id survives.

A damaged file is set aside and the list starts empty, like
remote-devices.yaml. One that cannot be read is kept, and nothing is
written over it until a later load reads it.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .identity import MACHINE_ID_RE
from .keychain import machine_key_account as machine_key_account
from .logging import get_logger
from .remote_devices import iso_utc, sanitize_device_name

log = get_logger("tstd.machines")

COMPUTERS_FILE = "computers.yaml"
MAX_MACHINES = 16
LAST_SEEN_THROTTLE_SECONDS = 60.0

Clock = Callable[[], float]

_FILE_VERSION = 1
_MCH_RE = re.compile(r"^mch_[0-9a-f]{12}$")
_DEVICE_RE = re.compile(r"^dev_[0-9a-f]{12}$")


class MachineLimitError(ValueError):
    """Raised when a pairing would go past ``MAX_MACHINES``."""


class MachinesUnavailableError(OSError):
    """Raised by a write while the file could not be read: never overwrite it."""


def mint_mch() -> str:
    return f"mch_{secrets.token_hex(6)}"


@dataclass(frozen=True)
class MachineRecord:
    """One paired computer. Never a key."""

    id: str
    name: str
    host: str
    port: int
    device_id: str
    paired_at: str
    machine_id: str | None = None
    last_seen: str | None = None


def _timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return iso_utc(value.timestamp())
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record_from(raw: object) -> MachineRecord | None:
    if not isinstance(raw, dict):
        return None
    mch, name, host, port = raw.get("id"), raw.get("name"), raw.get("host"), raw.get("port")
    device_id, machine_id = raw.get("device_id"), raw.get("machine_id")
    if not isinstance(mch, str) or not _MCH_RE.match(mch):
        return None
    if not isinstance(name, str) or not isinstance(host, str) or not host.strip():
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    if not isinstance(device_id, str) or not _DEVICE_RE.match(device_id):
        return None
    if machine_id is not None and (
        not isinstance(machine_id, str) or not MACHINE_ID_RE.match(machine_id)
    ):
        return None
    paired_at = _timestamp(raw.get("paired_at"))
    if paired_at is None:
        return None
    last_raw = raw.get("last_seen")
    last_seen = _timestamp(last_raw)
    if last_raw is not None and last_seen is None:
        return None
    return MachineRecord(
        id=mch,
        name=sanitize_device_name(name),
        host=host.strip(),
        port=port,
        device_id=device_id,
        paired_at=paired_at,
        machine_id=machine_id,
        last_seen=last_seen,
    )


def _entries(data: Any) -> list[Any] | None:
    if not isinstance(data, dict) or data.get("version") != _FILE_VERSION:
        return None
    machines = data.get("machines")
    if machines is None:
        return []
    return machines if isinstance(machines, list) else None


class MachineRegistry:
    """The computers this hub paired, in pairing order."""

    def __init__(self, data_dir: Path, clock: Clock = time.time) -> None:
        self._path = Path(data_dir) / COMPUTERS_FILE
        self._clock = clock
        self._machines: dict[str, MachineRecord] = {}
        self._last_written: dict[str, float] = {}
        self._unavailable = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def available(self) -> bool:
        return not self._unavailable

    @property
    def is_full(self) -> bool:
        return len(self._machines) >= MAX_MACHINES

    def __len__(self) -> int:
        return len(self._machines)

    def __contains__(self, mch: object) -> bool:
        return mch in self._machines

    def records(self) -> list[MachineRecord]:
        return list(self._machines.values())

    def get(self, mch: str) -> MachineRecord | None:
        return self._machines.get(mch)

    def by_machine_id(self, machine_id: str) -> MachineRecord | None:
        for record in self._machines.values():
            if record.machine_id == machine_id:
                return record
        return None

    def by_address(self, host: str, port: int) -> MachineRecord | None:
        for record in self._machines.values():
            if record.host == host and record.port == port:
                return record
        return None

    def load(self) -> None:
        """Read the file. A damaged one is set aside, never a failed start."""
        self._machines = {}
        self._last_written = {}
        try:
            data: Any = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._unavailable = False
            return
        except OSError as e:
            if not self._unavailable:
                log.warning(
                    "computers file could not be read; keeping it",
                    extra={
                        "extra_fields": {
                            "path": str(self._path),
                            "error": e.strerror or type(e).__name__,
                        }
                    },
                )
            self._unavailable = True
            return
        except (UnicodeDecodeError, yaml.YAMLError):
            data = None
        self._unavailable = False
        entries = _entries(data)
        if entries is None:
            self._set_aside()
            return
        skipped = 0
        for raw in entries:
            record = _record_from(raw)
            if record is None or record.id in self._machines:
                skipped += 1
                continue
            self._machines[record.id] = record
        if skipped:
            log.warning(
                "skipped malformed computer entries",
                extra={"extra_fields": {"skipped": skipped, "path": str(self._path)}},
            )

    def _set_aside(self) -> None:
        target = self._path.with_name(f"{COMPUTERS_FILE}.corrupt-{int(self._clock())}")
        try:
            os.replace(self._path, target)
        except OSError:
            log.warning(
                "computers file unreadable and could not be set aside",
                extra={"extra_fields": {"path": str(self._path)}},
            )
            return
        log.warning(
            "computers file unreadable; set aside and starting empty",
            extra={"extra_fields": {"path": str(target)}},
        )

    def _write(self, records: Iterable[MachineRecord]) -> None:
        if self._unavailable:
            raise MachinesUnavailableError(f"{self._path.name} could not be read")
        payload = {
            "version": _FILE_VERSION,
            "machines": [
                {
                    "id": r.id,
                    "machine_id": r.machine_id,
                    "name": r.name,
                    "host": r.host,
                    "port": r.port,
                    "device_id": r.device_id,
                    "paired_at": r.paired_at,
                    "last_seen": r.last_seen,
                }
                for r in records
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".computers.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def new_id(self) -> str:
        """A handle no record uses yet."""
        mch = mint_mch()
        while mch in self._machines:
            mch = mint_mch()
        return mch

    def put(self, record: MachineRecord) -> None:
        """Add or replace one record. The file is written first.

        A new record past ``MAX_MACHINES`` raises MachineLimitError. A
        failed write raises OSError and memory stays as it was.
        """
        if record.id not in self._machines and self.is_full:
            raise MachineLimitError(f"already {MAX_MACHINES} paired computers")
        updated = dict(self._machines)
        updated[record.id] = replace(record, name=sanitize_device_name(record.name))
        self._write(updated.values())
        self._machines = updated

    def remove(self, mch: str) -> bool:
        """Forget one computer. False when unknown; a failed write raises."""
        if mch not in self._machines:
            return False
        self._write(r for r in self._machines.values() if r.id != mch)
        del self._machines[mch]
        self._last_written.pop(mch, None)
        return True

    def touch_last_seen(self, mch: str, *, force: bool = False) -> bool:
        """Record that the computer answered; writes at most once a minute.

        *force* writes regardless (a disconnect). A failed write is logged.
        """
        record = self._machines.get(mch)
        if record is None:
            return False
        now = self._clock()
        last = self._last_written.get(mch)
        if not force and last is not None and now - last < LAST_SEEN_THROTTLE_SECONDS:
            return False
        try:
            self.put(replace(record, last_seen=iso_utc(now)))
        except OSError:
            log.warning(
                "could not record a computer's last_seen", extra={"extra_fields": {"machine": mch}}
            )
            return False
        self._last_written[mch] = now
        return True
