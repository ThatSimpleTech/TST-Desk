"""Whether computer use may run at all, read where every desktop tool is offered and run.

CU-01: "Enable computer use: Off" only wrote ~/.tst-cu-mcp/config.yaml; the
daemon's own desktop_* tools stayed registered, offered to the model and
dispatched. CU-07: Stop deliberately left capture working, so the model
could still take whole-screen screenshots while the switch read stopped.

The daemon keeps this gate current: Off (or a damaged policy, which reads
as Off) and Stop both close it. A closed gate means the desktop tools are
not offered, and a call that still comes is refused.
"""

from __future__ import annotations

from collections.abc import Callable

DESKTOP_PREFIX = "desktop_"

_reason: Callable[[], str | None] = lambda: None  # noqa: E731


def bind(reason: Callable[[], str | None]) -> None:
    """The daemon's answer: None when computer use may run, else why not."""
    global _reason
    _reason = reason


def unbind() -> None:
    bind(lambda: None)


def is_desktop_tool(name: str) -> bool:
    return name.startswith(DESKTOP_PREFIX)


def closed_reason() -> str | None:
    """Why the desktop tools may not run now, or None."""
    return _reason()
