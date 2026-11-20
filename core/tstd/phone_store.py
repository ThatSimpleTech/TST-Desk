"""The daemon's phone permissions, held in memory and on disk (TD-5279).

One :class:`PhoneStore` per daemon. ``phone_permissions`` is the file
format and the rules; this is the daemon's copy of both files plus the
shapes it hands to ``setup_state``, Doctor and the peer policy.

A write that fails leaves memory as it was: a grant that could not be
saved is not in force, and one that could not be cleared is still shown
as on, so what the person sees is what the next start will read.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import phone_permissions as perms
from .logging import get_logger
from .protocol import DiagnosticCheck, PhonePermissionState

log = get_logger("tstd.phone_store")

# What the notice quotes of a command or address: enough to recognise it.
_QUOTE_LIMIT = 200


def _clip(text: str) -> str:
    return text if len(text) <= _QUOTE_LIMIT else text[: _QUOTE_LIMIT - 1] + "…"


def server_saved_summary(server_id: str, command: Sequence[str], url: str, *, existed: bool) -> str:
    """What a phone did to a server, without the phone's name."""
    what = shlex.join(command) if command else url
    verb = "changed" if existed else "added"
    return f"{verb} MCP server '{server_id}': {_clip(what)}"


def value_summary(server_id: str, target: str, name: str, *, stored: bool) -> str:
    """A value stored or removed. The name only; the value is never here."""
    kind = "header" if target == "header" else "variable"
    verb = "stored a value for" if stored else "removed the value of"
    return f"{verb} {kind} '{name}' of MCP server '{server_id}'."


class PhoneStore:
    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._grants = perms.load_grants(self._data_dir)
        self._origins = perms.load_origins(self._data_dir)

    # ── grants ────────────────────────────────────────────────────────

    def live(self) -> frozenset[str]:
        """The capabilities a phone may use right now."""
        return perms.live(self._grants, perms.utc_now())

    def set(self, capability: str, enabled: bool, expires_in_seconds: int | None) -> None:
        """Switch one capability. ValueError on a bad ask, OSError on a failed write."""
        after = perms.changed(
            self._grants, capability, enabled, expires_in_seconds, perms.utc_now()
        )
        perms.save_grants(self._data_dir, after)
        self._grants = after

    def states(self) -> dict[str, PhonePermissionState]:
        """Every capability, on or off, as ``setup_state`` shows it."""
        now = perms.utc_now()
        out: dict[str, PhonePermissionState] = {}
        for name in perms.CAPABILITIES:
            grant = self._grants.get(name)
            if grant is not None and grant.live(now):
                out[name] = PhonePermissionState(enabled=True, expires_at=grant.expires_at)
            else:
                out[name] = PhonePermissionState()
        return out

    def next_expiry(self) -> datetime | None:
        return perms.next_expiry(self._grants, perms.utc_now())

    def doctor_row(self) -> DiagnosticCheck | None:
        """A warning while any phone permission is on; no row when all are off."""
        now = perms.utc_now()
        on: list[str] = []
        for name in perms.CAPABILITIES:
            grant = self._grants.get(name)
            if grant is None or not grant.live(now):
                continue
            until = f"until {grant.expires_at}" if grant.expires_at else "until turned off"
            on.append(f"{perms.LABELS[name]} ON ({until})")
        if not on:
            return None
        return DiagnosticCheck(
            name="phone_permissions",
            status="warn",
            detail="paired phones may change: " + "; ".join(on),
        )

    # ── where a server came from ──────────────────────────────────────

    def origin_of(self, server_id: str) -> perms.Origin | None:
        return self._origins.get(server_id)

    def servers_from(self, device_id: str) -> list[str]:
        return perms.servers_from(self._origins, device_id)

    def record_origin(self, server_id: str, device_id: str, device_name: str) -> None:
        after = dict(self._origins)
        after[server_id] = perms.Origin(
            device_id=device_id, device_name=device_name, at=perms.stamp(perms.utc_now())
        )
        self._save_origins(after)

    def forget_origin(self, server_id: str) -> None:
        if server_id not in self._origins:
            return
        after = dict(self._origins)
        del after[server_id]
        self._save_origins(after)

    def _save_origins(self, after: perms.Origins) -> None:
        # The server is already saved by now; a record that cannot be
        # written must not turn that into an error. It is logged instead.
        try:
            perms.save_origins(self._data_dir, after)
        except OSError:
            log.exception("could not write the MCP origins file")
            return
        self._origins = after
