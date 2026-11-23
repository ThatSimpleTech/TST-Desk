"""Backend interface, enforcement level and the probe cache (TD-5294)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

# What a sandbox can promise, each in one plain sentence. A backend reports
# the keys it does not enforce as gaps; one gap makes the level partial.
PROMISES: dict[str, str] = {
    "writes": "writes land only in the workspace and a private temp folder",
    "pins": "steering files, .tst and .git cannot be changed, replaced or renamed away",
    "hidden": "keys, credentials and TST Desk's own data cannot be read",
    "keychain": "the system keychain cannot be asked for a secret",
    "daemon": "TST Desk's data folder and its port cannot be reached",
    "network": "there is no network",
    "host_ipc": "services on your computer cannot be reached through sockets",
    "processes": "other programs on your computer cannot be seen or signalled",
}


class Enforcement(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True)
class BackendStatus:
    """What the sandbox on this computer enforces, as last tried."""

    backend: str
    enforcement: Enforcement
    # (promise key, why it is not enforced here)
    gaps: tuple[tuple[str, str], ...] = ()
    # Why it cannot run at all, with the fix. Set exactly when level is none.
    unavailable: str | None = None

    def gap_text(self) -> str:
        return "; ".join(
            f"{PROMISES.get(key, key)} is not enforced: {why}" for key, why in self.gaps
        )

    def card_line(self) -> str:
        """The approval card's one line for a command that runs in it."""
        if self.enforcement is Enforcement.FULL:
            return f"Runs in the sandbox ({self.backend}, full)"
        if self.enforcement is Enforcement.PARTIAL:
            keys = ", ".join(key for key, _ in self.gaps)
            return f"Runs in the sandbox ({self.backend}, partial: {keys} not enforced)"
        return "The sandbox cannot run here, so this command will be refused"


class SandboxBackend(Protocol):
    name: str

    def probe(self) -> BackendStatus:
        """Try the backend for real and say what it enforces. May be slow."""
        ...


_lock = threading.Lock()
_backend: SandboxBackend | None = None
_status: BackendStatus | None = None


def _default_backend() -> SandboxBackend:
    from .bwrap import BwrapBackend

    return BwrapBackend()


def set_backend(backend: SandboxBackend | None) -> None:
    """Use *backend* from now on (None: the default) and forget the cached probe."""
    global _backend, _status
    with _lock:
        _backend = backend
        _status = None


def current_status() -> BackendStatus:
    """The cached probe, probing once if nothing is cached. Blocking."""
    global _backend, _status
    with _lock:
        if _status is None:
            if _backend is None:
                _backend = _default_backend()
            _status = _backend.probe()
        return _status


def cached_status() -> BackendStatus | None:
    """The last probe without probing: for code on the event loop. Never blocks
    on a probe in progress; None until the first one has finished."""
    return _status


def reprobe() -> BackendStatus:
    """Probe again: Doctor was opened, or a sandboxed command failed to start.

    Installing bubblewrap then needs no restart.
    """
    global _status
    with _lock:
        _status = None
    return current_status()


def approval_view(
    status: BackendStatus | None, *, applies: bool
) -> tuple[str, str, bool, list[str], str] | None:
    """(level, backend, outside, gaps, line) for an approval card, or None
    when the probe has not answered yet."""
    if not applies:
        return ("none", "none", True, [], "Runs on your computer, outside the sandbox")
    if status is None:
        return None
    gaps = [f"{PROMISES.get(k, k)}: {why}" for k, why in status.gaps]
    return (status.enforcement.value, status.backend, False, gaps, status.card_line())
