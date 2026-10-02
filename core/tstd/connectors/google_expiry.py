"""Process-local mark that a Google refresh was rejected (TD-4854).

``invalid_grant`` means the grant is dead: a testing-mode client, an
unverified app, or a revoked token. The keychain item stays until
Disconnect or a new sign-in. This flag is what Settings reads so the
phase is expired rather than still connected. It is not a secret.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

_message: str | None = None
_hooks: list[Callable[[str], Awaitable[None]]] = []


def current_expired() -> str | None:
    """The reconnect sentence, or None when the last refresh succeeded."""
    return _message


def clear_expired() -> None:
    """A new grant or a disconnect retires the mark."""
    global _message
    _message = None


async def mark_expired(message: str) -> None:
    """Remember *message* and tell every live coordinator."""
    global _message
    _message = message
    for hook in list(_hooks):
        await hook(message)


def bind_expired(hook: Callable[[str], Awaitable[None]]) -> Callable[[], None]:
    """Register *hook*. The returned function removes it."""
    _hooks.append(hook)

    def unbind() -> None:
        try:
            _hooks.remove(hook)
        except ValueError:
            return

    return unbind
