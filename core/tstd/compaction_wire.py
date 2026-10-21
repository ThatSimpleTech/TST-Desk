"""Give a session its compaction summarizer (TD-5128).

The agent loop owns the worker client and the cost tracker, so it builds
the summarizer and leaves it on the session: the loop's own compactions
and ``/compact`` between turns both use it. Its worker calls stream, are
priced as worker calls off the user turn (as memory distill is), and show
in the cost meter. A summary still being written ahead is cancelled when
the loop ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .compaction_files import FileAccess
from .compaction_model import CompactionSummarizer
from .config import ModelConfig, TierConfig
from .cost import CostTracker
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderError,
    complete_streamed,
)

if TYPE_CHECKING:
    from .session import Session


def attach_summarizer(
    session: Session,
    config: ModelConfig,
    client_for: Callable[[TierConfig], Awaitable[Any]],
    tracker: CostTracker,
    access: Callable[[], FileAccess],
) -> CompactionSummarizer:
    """Build the session's summarizer, set it, and return it."""
    worker = config.tier("worker")

    async def complete(request: ChatCompletionRequest) -> ChatCompletionResponse | ProviderError:
        client = await client_for(worker)
        response = await complete_streamed(client, request)
        if not isinstance(response, ProviderError) and response.usage is not None:
            tracker.record_off_turn("worker", response.usage, worker, source="compaction")
            await session.event_log.add(tracker.emit_cost_update(session.id))
        return response

    summarizer = CompactionSummarizer(config.compaction, worker, complete, access)
    session.compaction_summarizer = summarizer
    current = asyncio.current_task()
    if current is not None:
        current.add_done_callback(lambda _task: summarizer.close())
    return summarizer
