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
//
// TD-1712 added the dispatcher for the rail's function entries. The entries
// themselves — and which of them can be clicked — live in the pure rail.ts.
//
// TD-1715 added the archived shelf and the open/confirm state the row
// affordances need. The commands those affordances send live in
// session-actions.svelte.ts; what stays here is the list, the filter, and the
// shelf split. One daemon list feeds both shelves — the daemon marks each row
// `archived` and the filter decides which shelf it lands on, so the recents
// menu and the chat pane keep reading the same complete event they always did.

import {
	onEvent,
	onConnectionState,
	sendToDaemon,
	ws,
} from "./connection-status.svelte.js";
import { chat, selectSession as selectChatSession } from "./chat-store.svelte.js";
import {
	focusSession,
	retargetWorkspace,
	session,
	workspaceName,
} from "./session-status.svelte.js";
import { toggleWorkspaceMenu } from "./workspaces.svelte.js";
import { RAIL_FUNCTIONS, type RailSurface } from "./rail";
import type { DaemonEventUnion, SessionSummary } from "./protocol";

/** localStorage key for the rail's collapsed flag. */
export const COLLAPSED_STORAGE_KEY = "tstdesk.sessionRailCollapsed";

export interface SessionRow {
	sessionId: string;
	workspacePath: string;
	state: SessionSummary["state"];
	/** Daemon's updated_at — drives newest-first ordering. Never clocks locally. */
	updatedAt: string;
	/** Filed away (TD-1715). Daemon truth; the rail only decides which shelf
	 *  it renders on. */
	archived: boolean;
}

export const sessions = $state({
	rows: [] as SessionRow[],
	filter: "",
	collapsed: false,
	/** Which shelf the history section is showing (TD-1715). */
	showArchived: false,
	/** Row whose action menu is open; one at a time. */
	menuFor: null as string | null,
	/** Row whose Delete is awaiting confirmation — Delete is irreversible, so
	 *  it never fires on the first click. */
	confirmDeleteFor: null as string | null,
	/** Row whose move-to-project picker is open. */
	moveFor: null as string | null,
	/** The daemon's refusal copy for the last lifecycle action, or null.
	 *  Rendered in the rail so the answer lands where the click did. */
	refusal: null as string | null,
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
	sessions.showArchived = false;
	closeRowMenus();
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
				archived: s.archived,
			}))
			.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
		// A move re-homes the bound session without unbinding it (TD-1715),
		// so the title bar's workspace has to follow the list rather than the
		// attach it never re-ran.
		const bound = sessions.rows.find((r) => r.sessionId === chat.sessionId);
		if (bound !== undefined) retargetWorkspace(bound.sessionId, bound.workspacePath);
		// Any list is an answer to something; a stale refusal outlives its click.
		sessions.refusal = null;
		closeRowMenus();
		return;
	}
	// A refused lifecycle action (TD-1715): the daemon owns "is a turn in
	// flight", so the copy it sends is the copy the rail shows — the UI never
	// second-guesses it or invents its own reason.
	if (event.type === "error" && event.code === "session_busy") {
		sessions.refusal = event.message;
		sessions.confirmDeleteFor = null;
		sessions.moveFor = null;
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

/** The rendered rows: the current shelf, newest first, narrowed by the
 *  filter. Matches the session id and the workspace path (its basename
 *  included via the path).
 *
 *  The shelf split is why archived sessions are "hidden from the default
 *  list" (TD-1715): one daemon list, one filter, two views — never a second
 *  request whose scope the other stores would also have to reason about. */
export function visibleRows(): SessionRow[] {
	const needle = sessions.filter.trim().toLowerCase();
	return sessions.rows.filter(
		(r) =>
			r.archived === sessions.showArchived &&
			(needle === "" ||
				r.sessionId.toLowerCase().includes(needle) ||
				r.workspacePath.toLowerCase().includes(needle)),
	);
}

/** Rows on the shelf being shown, before the filter — tells "nothing here"
 *  apart from "nothing matches". */
export function shelfRowCount(): number {
	return sessions.rows.filter((r) => r.archived === sessions.showArchived).length;
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

/** Close whatever row affordance is open (TD-1715). Lives here rather than
 *  with the actions themselves because the reducer above closes menus on
 *  every refreshed list, and a store must not import from its own consumer. */
export function closeRowMenus(): void {
	sessions.menuFor = null;
	sessions.confirmDeleteFor = null;
	sessions.moveFor = null;
	sessions.refusal = null;
}

export function toggleCollapsed(): void {
	sessions.collapsed = !sessions.collapsed;
	saveCollapsed(sessions.collapsed);
}

/** Activate a rail function entry (TD-1712). Returns false — having done
 *  nothing — for any entry the registry doesn't call `ready`.
 *
 *  The guard lives here and not only in the markup, so a rail that forgets to
 *  disable a row still can't produce a click that goes nowhere. Projects
 *  raises TD-1103's recents menu rather than growing a second picker. */
export function activateRailFunction(id: RailSurface): boolean {
	const entry = RAIL_FUNCTIONS.find((e) => e.id === id);
	if (entry === undefined || entry.state !== "ready") return false;
	if (id === "projects") {
		toggleWorkspaceMenu();
		return true;
	}
	// Marking an entry ready without wiring it here lands back here;
	// sessions.test.ts asserts every ready entry activates.
	return false;
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
