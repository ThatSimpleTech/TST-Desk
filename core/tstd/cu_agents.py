"""Which TST Desk session is acting, for each agent's own cursor (TD-5227).

Every session's computer use draws its own cursor on the real display, with
its own color and a label naming the chat. The session loop sets
:data:`current_cu_agent` once when it starts; asyncio copies context into
every task the loop spawns, so the drivers read the acting session without
any change to the tool signatures.

Colors are assigned by tst-cu-mcp's machine-wide registry (the sidecar
answers ``overlay_agent`` with one), so a TST Desk session never shares a
color with another live client. :func:`fallback_color` covers drivers with
no sidecar.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

#: Mirrors tst_cu_mcp.overlay.agents.PALETTE without a cross-package import.
PALETTE = (
    "#2F6FDE",
    "#16A34A",
    "#8B5CF6",
    "#DB2777",
    "#0891B2",
    "#4F46E5",
    "#65A30D",
    "#475569",
)
_LABEL_MAX = 40
_PREFIX = "TST Desk"

_title_lookup: Callable[[str], str | None] = lambda _sid: None  # noqa: E731


@dataclass(frozen=True)
class CuAgent:
    """One session as a computer-use agent."""

    session_id: str

    @property
    def agent_id(self) -> str:
        """Stable, short, and safe for agent-desktop's ``--agent-id``."""
        return "tst-" + "".join(ch for ch in self.session_id if ch.isalnum())[:12]

    @property
    def label(self) -> str:
        """``TST Desk · <chat title>``, at most 40 characters."""
        title = " ".join((_title_lookup(self.session_id) or "").split())
        if not title:
            return _PREFIX
        text = f"{_PREFIX} · {title}"
        return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 1].rstrip() + "…"


#: The session whose loop is running in this context, or None (no session).
current_cu_agent: ContextVar[CuAgent | None] = ContextVar("current_cu_agent", default=None)


def set_title_lookup(lookup: Callable[[str], str | None]) -> None:
    """The daemon's session store answers a session id with its chat title."""
    global _title_lookup
    _title_lookup = lookup


def fallback_color(agent_id: str) -> str:
    """A stable palette color when no sidecar assigns one."""
    return PALETTE[sum(agent_id.encode("utf-8")) % len(PALETTE)]


__all__ = [
    "PALETTE",
    "CuAgent",
    "current_cu_agent",
    "fallback_color",
    "set_title_lookup",
]
