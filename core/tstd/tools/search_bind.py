"""Which search block a call uses (TD-4843).

``web_search`` keeps zero-argument endpoint helpers so tests can replace
them. Those helpers ask here first. A daemon session sets the override
from ``session.config.search``; the tool registry closes over the same
block for classification. Unset means the fallback, which is
``cached_config()`` on the web_search module — the library path, and the
name those tests patch.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

from ..config import SearchConfig

_override: ContextVar[SearchConfig | None] = ContextVar("tstd_search_block", default=None)


def current_search(fallback: Callable[[], SearchConfig]) -> SearchConfig:
    """The bound block, or *fallback* when no daemon session set one."""
    bound = _override.get()
    if bound is not None:
        return bound
    return fallback()


def bind_search(search: SearchConfig | None) -> Token[SearchConfig | None] | None:
    """Install *search* for the current task. ``None`` leaves the fallback."""
    if search is None:
        return None
    return _override.set(search)


def reset_search(token: Token[SearchConfig | None] | None) -> None:
    if token is not None:
        _override.reset(token)
