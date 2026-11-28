"""A model stream that Stop and Cancel end at once (ERR2A-05).

The loop checked Stop only between chunks. While the provider waited on a
request that never answered, or slept between retries, no chunk came, so
nothing looked: on default settings a stopped turn ran on for eight minutes,
and the cancelled request was sent again three times. ``until_abort`` waits
for each chunk and for the session's own abort signal together; when Stop or
Cancel lands first, the pending wait is cancelled, which cancels whatever the
provider was waiting on (the request or the retry sleep), and the stream is
closed, which hangs up the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

T = TypeVar("T")


class Abortable(Protocol):
    async def wait_for_turn_abort(self) -> None: ...


async def until_abort(stream: AsyncIterator[T], session: Abortable) -> AsyncIterator[T]:
    """Yield *stream*'s items until it ends or the session's turn is aborted."""
    aborted = asyncio.ensure_future(session.wait_for_turn_abort())
    try:
        while True:
            item = asyncio.ensure_future(anext(stream))
            done, _ = await asyncio.wait({item, aborted}, return_when=asyncio.FIRST_COMPLETED)
            if item not in done:
                item.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await item
                return
            try:
                value = item.result()
            except StopAsyncIteration:
                return
            yield value
    finally:
        if not aborted.done():
            aborted.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await aborted
        close = getattr(stream, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
