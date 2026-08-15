// Session rail store (TD-1701).
//
// Owns the left rail's data: the daemon's session list (session_list events
// plus live session_state touches), the client-side filter, and the rail's
// collapsed state persisted to localStorage. Rows are keyed by session id
// and never invented client-side — the daemon's list is the record
// (AGENTS §6).
//
// Row actions re-target the two stores that follow one session each: the
// chat pane (attach + replay, TD-1004) and the title bar's session-scoped
// fields (TD-1006). Wiring mirrors the workspaces store: connection fan-out
// in, no client reference, no import cycle.

import {
	onEvent,
	onConnectionState,
	sendToDaemon,
	ws,
} from "./connection-status.svelte.js";
import { chat, selectSession as selectChatSession } from "./chat-store.svelte.js";
import { focusSession, session, workspaceName } from "./session-status.svelte.js";
import type { DaemonEventUnion, SessionSummary } from "./protocol";

/** localStorage key for the rail's collapsed flag. */
export const COLLAPSED_STORAGE_KEY = "tstdesk.sessionRailCollapsed";

export interface SessionRow {
	sessionId: string;
	workspacePath: string;
	state: SessionSummary["state"];
	/** Daemon's updated_at — drives newest-first ordering. Never clocks locally. */
	updatedAt: string;
}

export const sessions = $state({
	rows: [] as SessionRow[],
	filter: "",
	collapsed: false,
});

// While an anchor id sits here, the first session_state that isn't the
// attached session's own is the daemon's new_session reply — focus it on
// sight. A state flip on a session owned by another window could steal the
// focus in that window of a few frames; single-window use never notices.
let pendingNewAnchor: string | null = null;

// Coalesce refresh storms: a turn's state transitions each touch updated_at,
// but one in-flight list_sessions covers them all.
let listInFlight = false;

let started = false;

/** Register the reducer and the connect-time refresh. Idempotent; returns
 *  the unsubscribe for tests/teardown. */
export function startSessions(): () => void {
	if (started) return () => {};
	started = true;
	sessions.collapsed = loadCollapsed();
	const offEvents = onEvent(reduce);
	const offStates = onConnectionState((state) => {
		if (state === "connected") refresh();
	});
	// The rail can mount after the handshake completed; catch that case.
	if (ws.state === "connected") refresh();
	return () => {
		started = false;
		offEvents();
		offStates();
	};
}

/** Reset for tests (also releases the start guard so start re-loads storage). */
export function resetSessions(): void {
	started = false;
	pendingNewAnchor = null;
	listInFlight = false;
	sessions.rows = [];
	sessions.filter = "";
	sessions.collapsed = false;
}

/** Ask the daemon for the authoritative list (answer: session_list). */
export function refresh(): void {
	listInFlight = sendToDaemon({ type: "list_sessions" });
}

function requestRefreshIfIdle(): void {
	if (!listInFlight) refresh();
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "session_list") {
		listInFlight = false;
		sessions.rows = event.sessions
			.map((s) => ({
				sessionId: s.session_id,
				workspacePath: s.workspace_path,
				state: s.state,
				updatedAt: s.updated_at,
			}))
			.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
		return;
	}
	if (event.type !== "session_state") return;

	// The new_session reply arrives as the fresh session's first
	// session_state — before the refreshed list names it. Focus immediately.
	if (pendingNewAnchor !== null && event.session_id !== chat.sessionId) {
		const anchor = pendingNewAnchor;
		pendingNewAnchor = null;
		// The new session shares the anchor's workspace by definition; the
		// anchor's row is the authoritative path, session-status the fallback.
		const workspacePath =
			sessions.rows.find((r) => r.sessionId === anchor)?.workspacePath ??
			session.workspacePath ??
			undefined;
		selectChatSession(event.session_id, event.state);
		focusSession(event.session_id, event.state, workspacePath);
		refresh();
		return;
	}

	const row = sessions.rows.find((r) => r.sessionId === event.session_id);
	if (row === undefined) {
		// A session the list doesn't know yet (opened by another window, or
		// the post-open_workspace transition before the first list lands).
		requestRefreshIfIdle();
		return;
	}
	if (row.state !== event.state) {
		row.state = event.state;
		// States are daemon truth per row; ordering comes from the refreshed
		// list so newest-first stays the store's updated_at, not a local clock.
		requestRefreshIfIdle();
	}
}

/** The rendered rows: newest first, narrowed by the filter. Matches the
 *  session id and the workspace path (its basename included via the path). */
export function visibleRows(): SessionRow[] {
	const needle = sessions.filter.trim().toLowerCase();
	if (needle === "") return sessions.rows;
	return sessions.rows.filter(
		(r) =>
			r.sessionId.toLowerCase().includes(needle) ||
			r.workspacePath.toLowerCase().includes(needle),
	);
}

export function setFilter(value: string): void {
	sessions.filter = value;
}

/** Click a row: attach the window to that session. No-op when unknown or
 *  already attached — the chat store's own switch no-ops the same id too. */
export function selectRow(sessionId: string): void {
	const row = sessions.rows.find((r) => r.sessionId === sessionId);
	if (row === undefined || row.sessionId === chat.sessionId) return;
	selectChatSession(row.sessionId, row.state);
	focusSession(row.sessionId, row.state, row.workspacePath);
}

/** New-session action: a fresh session in the attached session's workspace.
 *  Focus moves when the daemon's reply (its first session_state) lands.
 *
 *  With nothing bound (TD-1711: auto-bind refuses terminal sessions, so a
 *  restart can leave the app unbound) the newest listed session is the
 *  anchor instead — the daemon only needs its workspace, and a tombstone
 *  anchor works. With no rows at all there is no workspace to anchor on. */
export function newSession(): boolean {
	if (pendingNewAnchor !== null) return false;
	const anchor = chat.sessionId ?? sessions.rows[0]?.sessionId ?? null;
	if (anchor === null) return false;
	if (!sendToDaemon({ type: "new_session", session_id: anchor })) return false;
	pendingNewAnchor = anchor;
	return true;
}

export function toggleCollapsed(): void {
	sessions.collapsed = !sessions.collapsed;
	saveCollapsed(sessions.collapsed);
}

// ── Row presentation (pure; the component renders, these decide) ────────

/** Short per-row state text (title bar wording, minus its "none"). */
export const ROW_STATE_LABELS: Record<SessionSummary["state"], string> = {
	idle: "Idle",
	running: "Running",
	awaiting_approval: "Awaiting approval",
	paused: "Paused",
	complete: "Complete",
	failed: "Failed",
	cancelled: "Cancelled",
	interrupted: "Interrupted",
};

/** Dot tone per state — the same mapping the title bar's indicator uses. */
export function stateTone(state: SessionSummary["state"]): "info" | "warning" | "danger" | "success" | "muted" {
	if (state === "running") return "info";
	if (state === "awaiting_approval" || state === "paused") return "warning";
	if (state === "failed") return "danger";
	if (state === "complete") return "success";
	return "muted";
}

/** Compact recency for a row ("now", 4m, 2h, 3d, then short date). The
 *  timestamp is the daemon's; only the phrasing is local. */
export function recencyLabel(iso: string, nowMs: number = Date.now()): string {
	const then = Date.parse(iso);
	if (Number.isNaN(then)) return "";
	const seconds = Math.max(0, Math.round((nowMs - then) / 1000));
	if (seconds < 60) return "now";
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours}h`;
	const days = Math.floor(hours / 24);
	if (days < 7) return `${days}d`;
	return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Row title: no session names exist yet (v0.3), so a short id it is. */
export function rowTitle(row: SessionRow): string {
	return row.sessionId.slice(0, 8);
}

/** Row subtitle: workspace name, recency — the "where and when" under the id. */
export function rowSubtitle(row: SessionRow, nowMs: number = Date.now()): string {
	const when = recencyLabel(row.updatedAt, nowMs);
	const where = workspaceName(row.workspacePath);
	return when === "" ? where : `${where} · ${when}`;
}

// ── Persistence (localStorage; guarded for node-env tests) ──────────────

function storage(): Storage | null {
	return typeof localStorage === "undefined" ? null : localStorage;
}

function loadCollapsed(): boolean {
	try {
		return storage()?.getItem(COLLAPSED_STORAGE_KEY) === "1";
	} catch {
		return false;
	}
}

function saveCollapsed(collapsed: boolean): void {
	try {
		storage()?.setItem(COLLAPSED_STORAGE_KEY, collapsed ? "1" : "0");
	} catch {
		// Storage unavailable (private mode quota, etc.) — the flag just won't
		// survive a restart. Not an error worth surfacing.
	}
}
