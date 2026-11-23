"""What a paired phone may change, as granted at the computer (TD-5279).

Some things a phone could ask for reach further than the session it is
looking at: an MCP server is a command this computer runs, at once and at
every session start, and it never meets the classifier. Those verbs are
refused from a phone unless the person at the computer switched the
matching capability on, after reading what it lets a phone do.

The grants live in ``<data dir>/phone-permissions.yaml``, beside
``remote-attach.yaml`` and for the same reason: it is this computer's own
state. It is not a ``config.yaml`` key and no workspace file is read for
it, so a repository can never switch a grant on.

Everything here fails closed. An absent, unreadable or malformed file, an
unknown capability, an entry that is not exactly ``enabled: true``, an
expiry that does not parse and an expiry in the past all read as off.

Also here: ``mcp-origins.yaml``, which remembers the phone a server was
added or changed from. Removing a phone does not undo what it changed, so
the record is what lets the computer show it and offer to remove it.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import yaml

# The closed set. A capability is added here together with its verbs in
# ``peer_policy.PHONE_OPT_IN`` and its own disclosure in Settings, Phones and devices.
MCP_SERVERS: Final = "mcp_servers"
# Turning approval prompts off for every session (TD-5283). Turning them
# back on needs nothing: that only tightens.
APPROVAL_SWITCHES: Final = "approval_switches"
# Adding a model or a key, or pointing a key at a host (TD-5296): the
# conversation and the key then go wherever that entry says.
MODELS_AND_KEYS: Final = "models_and_keys"
CAPABILITIES: Final[tuple[str, ...]] = (
    MCP_SERVERS,
    APPROVAL_SWITCHES,
    MODELS_AND_KEYS,
)

# What the refusal and the Doctor row call each one.
LABELS: Final[Mapping[str, str]] = {
    MCP_SERVERS: "MCP servers",
    APPROVAL_SWITCHES: "Turning approvals off",
    MODELS_AND_KEYS: "Adding models and keys",
}

# A timed grant is for "while I am out", not a second way to say forever.
MAX_EXPIRY_SECONDS: Final = 7 * 24 * 60 * 60

_FILE_VERSION: Final = 1
_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    """Now, in UTC. The one clock this module reads; tests replace it."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


def parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class Grant:
    """One capability switched on. Off is the absence of a grant."""

    granted_at: str
    expires_at: str | None = None

    def live(self, now: datetime) -> bool:
        if self.expires_at is None:
            return True
        until = parse_stamp(self.expires_at)
        return until is not None and now < until


Grants = dict[str, Grant]


def permissions_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "phone-permissions.yaml"


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_mapping(path: Path, fields: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(fields, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_grants(data_dir: str | Path) -> Grants:
    """The grants on file. Anything that is not plainly a grant is left out."""
    data = _read_mapping(permissions_path(data_dir))
    if data.get("version") != _FILE_VERSION:
        return {}
    raw = data.get("grants")
    if not isinstance(raw, dict):
        return {}
    grants: Grants = {}
    for name in CAPABILITIES:
        entry = raw.get(name)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        expires = entry.get("expires_at")
        if expires is not None and parse_stamp(expires) is None:
            continue
        granted = entry.get("granted_at")
        grants[name] = Grant(
            granted_at=granted if isinstance(granted, str) else "",
            expires_at=expires,
        )
    return grants


def save_grants(data_dir: str | Path, grants: Grants) -> None:
    """Write *grants* atomically. Every capability is listed, on or off."""
    body: dict[str, Any] = {}
    for name in CAPABILITIES:
        grant = grants.get(name)
        if grant is None:
            body[name] = {"enabled": False}
        else:
            body[name] = {
                "enabled": True,
                "granted_at": grant.granted_at,
                "expires_at": grant.expires_at,
            }
    _write_mapping(permissions_path(data_dir), {"version": _FILE_VERSION, "grants": body})


def changed(
    grants: Grants,
    capability: str,
    enabled: bool,
    expires_in_seconds: int | None,
    now: datetime,
) -> Grants:
    """*grants* with one capability switched. Raises ValueError on a bad ask."""
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown phone permission: {capability}")
    after = dict(grants)
    if not enabled:
        after.pop(capability, None)
        return after
    expires_at: str | None = None
    if expires_in_seconds is not None:
        if not 0 < expires_in_seconds <= MAX_EXPIRY_SECONDS:
            raise ValueError("A timed phone permission lasts at most seven days.")
        expires_at = stamp(now + timedelta(seconds=expires_in_seconds))
    after[capability] = Grant(granted_at=stamp(now), expires_at=expires_at)
    return after


def live(grants: Grants, now: datetime) -> frozenset[str]:
    """The capabilities a phone may use right now."""
    return frozenset(name for name, grant in grants.items() if grant.live(now))


def next_expiry(grants: Grants, now: datetime) -> datetime | None:
    """When the soonest still-live timed grant runs out."""
    ends = [
        until
        for grant in grants.values()
        if grant.live(now) and (until := parse_stamp(grant.expires_at)) is not None
    ]
    return min(ends) if ends else None


# ── where a server came from ──────────────────────────────────────────


@dataclass(frozen=True)
class Origin:
    """The phone an MCP server was last added or changed from."""

    device_id: str
    device_name: str
    at: str


Origins = dict[str, Origin]


def origins_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "mcp-origins.yaml"


def load_origins(data_dir: str | Path) -> Origins:
    data = _read_mapping(origins_path(data_dir))
    raw = data.get("servers") if data.get("version") == _FILE_VERSION else None
    if not isinstance(raw, dict):
        return {}
    found: Origins = {}
    for server_id, entry in raw.items():
        if not isinstance(server_id, str) or not isinstance(entry, dict):
            continue
        device_id, name, at = entry.get("device_id"), entry.get("device_name"), entry.get("at")
        if isinstance(device_id, str) and isinstance(name, str) and isinstance(at, str):
            found[server_id] = Origin(device_id=device_id, device_name=name, at=at)
    return found


def save_origins(data_dir: str | Path, origins: Origins) -> None:
    servers = {
        server_id: {"device_id": o.device_id, "device_name": o.device_name, "at": o.at}
        for server_id, o in sorted(origins.items())
    }
    _write_mapping(origins_path(data_dir), {"version": _FILE_VERSION, "servers": servers})


def servers_from(origins: Origins, device_id: str) -> list[str]:
    """The servers one device added or changed, by id."""
    return sorted(sid for sid, origin in origins.items() if origin.device_id == device_id)
