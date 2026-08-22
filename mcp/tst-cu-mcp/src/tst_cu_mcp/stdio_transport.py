"""Stdio transport that answers every request received before end-of-file.

The stock mcp stdio plumbing tears the serving task group down as soon as
stdin reports EOF. A client that pipelines several JSON-RPC lines in one
write - initialize, the initialized notification, and tools/list in a single
gulp - can have its last request dispatched to a freshly spawned handler in
the very scheduling quantum in which EOF arrives; teardown wins that race,
the handler is cancelled before its first step, and the process exits 0
having answered only the earlier requests (TD-4822). Paced clients never hit
it because the reader parks between lines, leaving the loop free to finish
each handler long before EOF.

:class:`DrainingStdioServer` interposes two relay streams between
:func:`stdio_server` and the lowlevel server, counts requests forwarded
against answers written, and holds the server-side read stream open after
EOF until every pre-EOF request has settled - answered on the wire, or
retired unanswered through the ``on_request_unanswered`` metadata hook the
dispatcher already calls for peer-cancelled work. Shutdown order then
follows the protocol contract ("no more input") instead of a scheduling
race.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
from mcp.server import MCPServer
from mcp.server.stdio import stdio_server
from mcp.shared.message import MessageMetadata, ServerMessageMetadata, SessionMessage
from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse


class DrainingStdioServer(MCPServer):
    """An :class:`MCPServer` whose stdio transport drains before signalling EOF."""

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (wire_read, wire_write):
            # Plain memory streams satisfy the SDK's ReadStream/WriteStream
            # protocols; consumers that want per-message contextvars duck-type
            # `last_context` off the stream and fall back cleanly without it.
            to_server_send, to_server_receive = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](0)
            from_server_send, from_server_receive = anyio.create_memory_object_stream[
                SessionMessage
            ](0)

            outstanding = 0
            settled = anyio.Condition()

            def request_forwarded() -> None:
                nonlocal outstanding
                outstanding += 1

            async def request_settled() -> None:
                nonlocal outstanding
                outstanding -= 1
                async with settled:
                    settled.notify_all()

            async def read_relay() -> None:
                """Forward frames toward the server, then hold EOF until drained."""
                async with to_server_send:
                    async for item in wire_read:
                        if (
                            isinstance(item, SessionMessage)
                            and isinstance(item.message, JSONRPCRequest)
                            and _stamp_unanswered_hook(item, request_settled)
                        ):
                            request_forwarded()
                        await to_server_send.send(item)
                # stdin is exhausted. The stock plumbing would let that cascade
                # into the serving task group immediately, cancelling handlers
                # spawned moments ago mid-flight. Hold this stream open until
                # every forwarded request has an answer on the wire or has
                # settled unanswered, then close it so shutdown proceeds.
                async with settled:
                    await settled.wait_for(lambda: outstanding == 0)

            async def write_relay() -> None:
                """Count answers on their way out so the drain gate can lift."""
                async with wire_write:
                    async for message in from_server_receive:
                        # Settle before the send: a response already counted as
                        # delivered keeps the gate honest even if the transport
                        # tears down under the write during shutdown.
                        if isinstance(message.message, JSONRPCResponse | JSONRPCError):
                            await request_settled()
                        try:
                            await wire_write.send(message)
                        except (
                            anyio.BrokenResourceError,
                            anyio.ClosedResourceError,
                        ):
                            return

            async with anyio.create_task_group() as tg:
                tg.start_soon(read_relay)
                tg.start_soon(write_relay)
                await self._lowlevel_server.run(
                    to_server_receive,
                    from_server_send,
                    self._lowlevel_server.create_initialization_options(),
                )


def _stamp_unanswered_hook(
    item: SessionMessage,
    hook: Callable[[], Awaitable[None]],
) -> bool:
    """Attach `hook` where the dispatcher reports a request settling unanswered.

    Stdio frames carry no metadata, so this normally builds a fresh
    ``ServerMessageMetadata``. Returns False for the shapes that cannot host
    the hook (none occur on stdio); those requests stay ungated rather than
    risking a drain that never lifts.
    """
    metadata: MessageMetadata | None = item.metadata
    if metadata is None:
        item.metadata = ServerMessageMetadata(on_request_unanswered=hook)
        return True
    if isinstance(metadata, ServerMessageMetadata) and metadata.on_request_unanswered is None:
        metadata.on_request_unanswered = hook
        return True
    return False
