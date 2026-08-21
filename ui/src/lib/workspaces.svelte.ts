// Recent-workspaces store (TD-1103).
//
// The recents list is already on the wire: every `session_list` event
// carries `workspace_path` + `updated_at` per session, and the daemon's
// session store is the durable record. This store dedupes those paths
// (newest first) into the quick-switch menu in the title bar — no new
// protocol. Per-entry removal is UI-only, persisted to localStorage as a
// hide-list: it never deletes the daemon's session history.
//
// Wiring mirrors the doctor/decisions stores: connection fan-out in, no
// client reference, no import cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { openWorkspace } from "./session-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

/** localStorage key for paths the user removed from the menu. */
export const HIDDEN_STORAGE_KEY = "tstdesk.hiddenRecentWorkspaces";

export interface RecentWorkspace {
	/** Absolute workspace path (the switch payload). */
	path: string;
	/** ISO timestamp of the latest session touching this path. */
	lastSeen: string;
}

export const workspaces = $state({
	menuOpen: false,
	/** All known paths, newest first — including hidden ones. */
	entries: [] as RecentWorkspace[],
	/** Paths the user removed; persisted to localStorage. */
	hidden: [] as string[],
	/** Machine-wide pins (TD-2806). From setup_state, not localStorage. */
	pinned: [] as string[],
});

let started = false;

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startWorkspaces(): () => void {
	if (started) return () => {};
	started = true;
	workspaces.hidden = loadHidden();
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests (also releases the start guard so start re-loads storage). */
export function resetWorkspaces(): void {
	started = false;
	workspaces.menuOpen = false;
	workspaces.entries = [];
	workspaces.hidden = [];
	workspaces.pinned = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "setup_state") {
		workspaces.pinned = event.pinned_workspaces ?? [];
		return;
	}
	if (event.type !== "session_list") return;
	// Dedupe sessions by workspace path, keeping the newest update per path.
	const byPath = new Map<string, string>();
	for (const s of event.sessions) {
		const prev = byPath.get(s.workspace_path);
		if (prev === undefined || s.updated_at > prev) byPath.set(s.workspace_path, s.updated_at);
	}
	workspaces.entries = [...byPath.entries()]
		.map(([path, lastSeen]) => ({ path, lastSeen }))
		.sort((a, b) => (a.lastSeen < b.lastSeen ? 1 : -1))
		.slice(0, 12);
}

/** The menu rows: recents minus the user's removals, newest first. */
export function visibleRecents(): RecentWorkspace[] {
	return workspaces.entries.filter((r) => !workspaces.hidden.includes(r.path));
}

/** Switch to a recent workspace and close the menu. */
export function openRecent(path: string): void {
	workspaces.menuOpen = false;
	openWorkspace(path);
}

/** Remove a path from the menu (UI-only; daemon history is untouched). */
/** Pin or unpin a workspace. The daemon acks with setup_state. */
export function setWorkspacePin(path: string, pinned: boolean): void {
	sendToDaemon({ type: "set_workspace_pin", path, pinned });
}

export function hideRecent(path: string): void {
	if (workspaces.hidden.includes(path)) return;
	workspaces.hidden = [...workspaces.hidden, path];
	saveHidden(workspaces.hidden);
}

export function toggleWorkspaceMenu(): void {
	workspaces.menuOpen = !workspaces.menuOpen;
}

export function closeWorkspaceMenu(): void {
	workspaces.menuOpen = false;
}

// ── Persistence (localStorage; guarded for node-env tests) ──────────────

function storage(): Storage | null {
	return typeof localStorage === "undefined" ? null : localStorage;
}

function loadHidden(): string[] {
	try {
		const raw = storage()?.getItem(HIDDEN_STORAGE_KEY);
		if (!raw) return [];
		const parsed: unknown = JSON.parse(raw);
		return Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === "string") : [];
	} catch {
		return [];
	}
}

function saveHidden(paths: string[]): void {
	try {
		storage()?.setItem(HIDDEN_STORAGE_KEY, JSON.stringify(paths));
	} catch {
		// Storage unavailable (private mode quota, etc.) — the list just
		// won't survive a restart. Not an error worth surfacing.
	}
}
