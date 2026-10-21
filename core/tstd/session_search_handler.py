"""The daemon's answer to ``search_sessions``: the rail's search box (TD-5129).

The rail filters its rows on titles and paths by itself; this adds the
words the sessions held. Every session this computer's store knows is
searched, one best match each, so the rail can keep its own order and
show why a row matched. Read from the local index only (prime
directive 3).
"""

from __future__ import annotations

import asyncio

from .protocol import SearchSessions, SessionSearchHit, SessionSearchResults
from .session_index import IndexUnavailable, SessionIndex
from .session_store import SessionStore


async def search_sessions_reply(
    index: SessionIndex, store: SessionStore, msg: SearchSessions
) -> str:
    """The ``session_search_results`` JSON for *msg*."""
    known = {record.session_id for record in store.records()}
    try:
        hits = await asyncio.to_thread(
            index.search, msg.query, session_ids=known, limit=msg.limit, per_session=1
        )
    except IndexUnavailable:
        return SessionSearchResults(query=msg.query, available=False).model_dump_json()
    return SessionSearchResults(
        query=msg.query,
        hits=[
            SessionSearchHit(
                session_id=hit.session_id, turn=hit.turn, role=hit.role, snippet=hit.snippet
            )
            for hit in hits
        ],
    ).model_dump_json()
