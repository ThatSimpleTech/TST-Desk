"""Who is acting: one cursor identity per agent (TD-5227).

Every computer-use client gets its own cursor color and label on the real
display. A client is one tst-cu-mcp process (another assistant's copy,
Grok's copy), or one TST Desk session inside the daemon's shared sidecar,
which names itself with the internal ``overlay_agent`` tool before acting.

Colors come from a machine-wide slot registry, so two live clients never
share a color while there are colors left: slot N is a lock file held with
a non-blocking ``flock`` by whichever process claimed it. The OS drops the
lock when that process exits, so a crashed client frees its color without
any cleanup. Past the last slot a color repeats; the label still tells the
agents apart.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Cursor fills, distinct from each other and from the rust ring.
PALETTE = (
    "#2F6FDE",  # blue
    "#16A34A",  # green
    "#8B5CF6",  # violet
    "#DB2777",  # pink
    "#0891B2",  # teal
    "#4F46E5",  # indigo
    "#65A30D",  # olive
    "#475569",  # slate
)

#: The key a client uses until it names an agent (outside clients always do).
CLIENT_KEY = "client"

_KEY_OK = re.compile(r"[^A-Za-z0-9_-]")
_LABEL_MAX = 40
#: The connected MCP client's label: every agent's until it is named.
_client_label = "Agent"


def set_client_name(name: str) -> None:
    """Record ``clientInfo.name`` from the handshake (the stdio relay does)."""
    global _client_label
    if name.strip():
        _client_label = label_for_client(name)


def client_label() -> str:
    return _client_label


@dataclass(frozen=True)
class Agent:
    """One cursor: a stable key, the text beside it, and its fill."""

    key: str
    label: str
    color: str


def clean_key(raw: str) -> str:
    """Letters, digits, ``-`` and ``_`` only (it travels on a line protocol)."""
    key = _KEY_OK.sub("", raw)[:64]
    return key or CLIENT_KEY


def clean_label(raw: str) -> str:
    """One line, trimmed, at most 40 characters."""
    label = " ".join(raw.split())
    return label[:_LABEL_MAX].rstrip() or "Agent"


def label_for_client(name: str) -> str:
    """A readable label from an MCP client's ``clientInfo.name``."""
    low = name.casefold()
    for needle, label in (
        ("claude", "Claude"),
        ("grok", "Grok"),
        ("tst-desk", "TST Desk"),
        ("tstd", "TST Desk"),
        ("codex", "Codex"),
        ("cursor", "Cursor"),
    ):
        if needle in low:
            return label
    words = re.sub(r"[-_.]+", " ", name).strip()
    return clean_label(words.title()) if words else "Agent"


class SlotRegistry:
    """Machine-wide color slots: one held lock file per live agent."""

    def __init__(self, directory: Path | None = None, size: int = len(PALETTE)) -> None:
        self._directory = directory
        self._size = size
        self._held: dict[str, tuple[int, int]] = {}

    def _dir(self) -> Path:
        if self._directory is not None:
            return self._directory
        from tst_cu_mcp.config import policy_path

        return policy_path().parent / "agents"

    def claim(self, key: str) -> int:
        """This key's slot, claiming the lowest free one the first time."""
        if key in self._held:
            return self._held[key][0]
        try:
            import fcntl
        except ImportError:
            return _fallback_slot(key, self._size)
        directory = self._dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _fallback_slot(key, self._size)
        for slot in range(self._size):
            try:
                fd = os.open(directory / f"slot-{slot}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            self._held[key] = (slot, fd)
            return slot
        return _fallback_slot(key, self._size)

    def release(self, key: str) -> None:
        """Give the key's slot back (closing the file drops the lock)."""
        held = self._held.pop(key, None)
        if held is not None:
            with contextlib.suppress(OSError):
                os.close(held[1])

    def release_all(self) -> None:
        for key in list(self._held):
            self.release(key)


def _fallback_slot(key: str, size: int) -> int:
    """A stable slot when no lock can be taken (no fcntl, unwritable dir)."""
    return sum(key.encode("utf-8")) % max(1, size)


class AgentBook:
    """This process's agents and which one is acting now."""

    def __init__(self, registry: SlotRegistry | None = None) -> None:
        self._registry = registry or SlotRegistry()
        self._labels: dict[str, str] = {}
        self._current = CLIENT_KEY

    def use(self, key: str, label: str = "") -> Agent:
        """Make ``key`` the acting agent (labelled ``label`` if given)."""
        clean = clean_key(key)
        if label.strip():
            self._labels[clean] = clean_label(label)
        self._current = clean
        return self.current()

    def current(self) -> Agent:
        key = self._current
        label = self._labels.get(key, client_label())
        color = PALETTE[self._registry.claim(key) % len(PALETTE)]
        return Agent(key=key, label=label, color=color)

    def close(self) -> None:
        self._registry.release_all()


__all__ = [
    "CLIENT_KEY",
    "PALETTE",
    "Agent",
    "AgentBook",
    "SlotRegistry",
    "clean_key",
    "clean_label",
    "client_label",
    "label_for_client",
    "set_client_name",
]
