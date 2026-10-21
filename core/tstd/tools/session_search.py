"""The ``session_search`` tool: recall earlier sessions in this workspace (TD-5129).

The model asks in its own words ("what did we decide about the date
parser?") and gets the best matching messages from the workspace's other
sessions, each cited by session and turn so the person can open the one
it came from. The search runs over the local full-text index
(:mod:`tstd.session_index`); nothing is sent anywhere (prime directive 3).

Scope is the calling session's project, not the whole computer: a session
in another workspace is another job, and its text is not this one's
business. The session itself is left out, since its own words are already
in the model's context.

Reading the index is all it does, so the classifier's ``session-search``
rule puts the call in Class A.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

from ..session_index import RECALL_SNIPPET_TOKENS, IndexUnavailable, RecallHit, SessionIndex
from ..session_store import SessionRecord
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

SESSION_SEARCH_NAME = "session_search"
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10
#: Matches taken from one session, so one long session cannot fill the list.
PER_SESSION = 2
SHORT_ID = 8

SESSION_SEARCH_DESCRIPTION = (
    "Search what was said in earlier sessions in this workspace: the user's messages and "
    "your replies. Use it when the user refers to past work ('like we did last week', "
    "'what did we decide about X') or when an earlier decision would help. query is a few "
    "keywords. Each result cites the session and turn it came from; cite them the same "
    "way when you use one. Searches this computer only."
)

SESSION_SEARCH_TOOL = Tool(
    name=SESSION_SEARCH_NAME,
    description=SESSION_SEARCH_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Keywords to look for, e.g. 'date parser timezone'",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS_CAP,
                "description": f"Most matches to return (default {DEFAULT_MAX_RESULTS})",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=True,
    mutates=False,
)


@dataclass(frozen=True)
class SessionRecall:
    """The index plus the session store rows that scope and label its hits."""

    index: SessionIndex
    records: Callable[[], Iterable[SessionRecord]]

    def recall(self, session_id: str, query: str, limit: int) -> list[RecallHit]:
        """Hits from the other sessions in *session_id*'s workspace, best first.

        Sync: the handler runs it in a thread. Raises
        :class:`IndexUnavailable` when the index is off.
        """
        rows = list(self.records())
        own = next((r for r in rows if r.session_id == session_id), None)
        if own is None:
            return []
        peers = {
            r.session_id: r
            for r in rows
            if r.workspace_path == own.workspace_path and r.session_id != session_id
        }
        hits = self.index.search(
            query,
            session_ids=peers.keys(),
            limit=limit,
            per_session=PER_SESSION,
            snippet_tokens=RECALL_SNIPPET_TOKENS,
        )
        return [
            RecallHit(
                session_id=hit.session_id,
                title=peers[hit.session_id].title,
                updated_at=peers[hit.session_id].updated_at,
                turn=hit.turn,
                turn_id=hit.turn_id,
                role=hit.role,
                snippet=hit.snippet,
            )
            for hit in hits
        ]


def register_session_search_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``session_search`` tool. Call from ``create_registry``."""
    registry.register(SESSION_SEARCH_TOOL)


def register_session_search_handler(
    dispatcher: ToolDispatcher, recall: SessionRecall | None
) -> None:
    """Bind the handler; with no *recall* it exists and refuses."""
    dispatcher.register_handler(SESSION_SEARCH_NAME, partial(session_search, recall=recall))


def format_hits(query: str, hits: list[RecallHit]) -> str:
    """The tool result: one cited line per match."""
    if not hits:
        return (
            f"No earlier session in this workspace mentions {query.strip()!r}. Try other keywords."
        )
    lines = [f"{len(hits)} match(es) from earlier sessions in this workspace, best first:"]
    for hit in hits:
        title = f' "{hit.title}"' if hit.title else ""
        who = "user" if hit.role == "user" else "assistant"
        lines.append(
            f"- [session {hit.session_id[:SHORT_ID]}{title}, turn {hit.turn}, "
            f"last used {hit.updated_at[:10]}] {who}: {hit.snippet}"
        )
    lines.append("Cite a match as [session <id>, turn <n>] when you rely on it.")
    return "\n".join(lines)


async def session_search(
    *,
    recall: SessionRecall | None,
    session: object = None,
    tool_call_id: str = "",
    query: str,
    max_results: int | None = None,
) -> str:
    """Search the workspace's other sessions for *query*."""
    session_id = getattr(session, "id", None)
    if recall is None or not isinstance(session_id, str):
        raise HandlerRefusal(
            "session_search_unavailable", "Session search is not available in this session."
        )
    limit = min(max(max_results or DEFAULT_MAX_RESULTS, 1), MAX_RESULTS_CAP)
    try:
        hits = await asyncio.to_thread(recall.recall, session_id, query, limit)
    except IndexUnavailable as e:
        raise HandlerRefusal(
            "session_search_unavailable",
            "The session search index could not be opened, so earlier sessions "
            "cannot be searched right now.",
        ) from e
    return format_hits(query, hits)
