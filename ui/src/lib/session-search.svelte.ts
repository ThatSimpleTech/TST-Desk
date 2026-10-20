// What the rail's search box finds inside sessions (TD-5129).
//
// The rail filters titles, ids and paths itself; the words a session held
// are only on the daemon, in its local full-text index. Typing sends a
// debounced `search_sessions`, and the reply names, per session, the best
// matching message. Only a reply to the text still in the box counts: a
// slower answer to an earlier keystroke is dropped, never shown as if it
// matched what is typed now (AGENTS §6: the UI derives nothing).

import { sendToDaemon } from "./connection-status.svelte.js";
import type { SessionSearchHit, SessionSearchResults } from "./protocol";

/** Shorter queries match nearly everything; the rail's own filter covers them. */
export const MIN_QUERY_CHARS = 2;
/** Wait this long after the last keystroke before asking. */
export const SEARCH_DEBOUNCE_MS = 200;
/** Rows the daemon returns at most, one per session. */
export const SEARCH_LIMIT = 50;

export const contentSearch = $state({
	/** The query the hits answer; "" when there are none. */
	query: "",
	hits: {} as Record<string, SessionSearchHit>,
	/** False when the daemon's index could not be opened. */
	available: true,
});

let wanted = "";
let timer: ReturnType<typeof setTimeout> | null = null;

function clearHits(): void {
	contentSearch.query = "";
	contentSearch.hits = {};
}

/** Take a `session_search_results` reply; the rail store routes it here. */
export function applySearchResults(event: SessionSearchResults): void {
	if (event.query !== wanted) return;
	const hits: Record<string, SessionSearchHit> = {};
	for (const hit of event.hits) hits[hit.session_id] = hit;
	contentSearch.hits = hits;
	contentSearch.query = event.query;
	contentSearch.available = event.available;
}

/** The box changed: ask the daemon once typing pauses. */
export function searchContent(filter: string): void {
	const query = filter.trim();
	if (timer !== null) {
		clearTimeout(timer);
		timer = null;
	}
	wanted = query.length >= MIN_QUERY_CHARS ? query : "";
	if (wanted === "") {
		clearHits();
		return;
	}
	timer = setTimeout(() => {
		timer = null;
		sendToDaemon({ type: "search_sessions", query: wanted, limit: SEARCH_LIMIT });
	}, SEARCH_DEBOUNCE_MS);
}

/** The matching message of *sessionId* for the text in the box, or null. */
export function contentHit(sessionId: string, filter: string): SessionSearchHit | null {
	const query = filter.trim();
	if (query === "" || contentSearch.query !== query) return null;
	return contentSearch.hits[sessionId] ?? null;
}

/** Reset for tests. */
export function resetSessionSearch(): void {
	if (timer !== null) clearTimeout(timer);
	timer = null;
	wanted = "";
	clearHits();
	contentSearch.available = true;
}
