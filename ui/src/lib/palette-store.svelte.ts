// Command palette store (TD-1707).
//
// Holds the palette's open/query/selection state, composes the registry's
// fixed actions with the daemon's live sessions, and dispatches a chosen
// entry to the function that already owns that behavior. Nothing here
// reimplements a surface: new-session and attach go through the sessions
// store's exported actions, doctor/decisions/settings through theirs. A
// palette is a second doorway, not a second implementation.
//
// The matching and the registry are pure — see palette.ts.

import {
	ACTION_ENTRIES,
	nextIndex,
	rankEntries,
	type PaletteCommand,
	type PaletteEntry,
} from "./palette";
import { openDecisions } from "./decisions.svelte.js";
import { runDoctor } from "./doctor.svelte.js";
import { openSettings, setTheme, settings } from "./settings.svelte.js";
import { showRightPane } from "./right-pane.svelte.js";
import {
	newSession,
	selectRow,
	sessions,
	rowSubtitle,
	rowTitle,
	ROW_STATE_LABELS,
} from "./sessions.svelte.js";

export const palette = $state({
	open: false,
	query: "",
	/** Index into visibleEntries(). Reset by every query change. */
	index: 0,
});

/** One entry per listed session — the palette's attach action. Rows are the
 *  daemon's list; the palette never invents a session (AGENTS §6). */
function sessionEntries(): PaletteEntry[] {
	return sessions.rows.map((row) => ({
		id: `session:${row.sessionId}`,
		title: `Attach to ${rowTitle(row)}`,
		subtitle: `${rowSubtitle(row)} · ${ROW_STATE_LABELS[row.state]}`,
		icon: "message-square" as const,
		keywords: `${row.sessionId} ${row.workspacePath} session switch`,
		command: { kind: "attach", sessionId: row.sessionId },
	}));
}

/** Everything commandable right now: actions first, then sessions. */
export function paletteEntries(): PaletteEntry[] {
	return [...ACTION_ENTRIES, ...sessionEntries()];
}

/** The rendered list — matching entries, best first. */
export function visibleEntries(): PaletteEntry[] {
	return rankEntries(paletteEntries(), palette.query);
}

// ── Opening and keyboard state ────────────────────────────────────────

/** ⌘K. Opens on a clean query so the previous search never lingers. */
export function openPalette(): void {
	palette.open = true;
	palette.query = "";
	palette.index = 0;
}

export function closePalette(): void {
	palette.open = false;
	palette.query = "";
	palette.index = 0;
}

export function setPaletteQuery(query: string): void {
	palette.query = query;
	// The old index pointed into the old list; keeping it would leave the
	// highlight on whatever happened to land in that slot.
	palette.index = 0;
}

/** ↑/↓. Wraps, so the list is traversable without reaching for the mouse. */
export function movePaletteSelection(delta: number): void {
	palette.index = nextIndex(palette.index, delta, visibleEntries().length);
}

// ── Running ───────────────────────────────────────────────────────────

function dispatch(command: PaletteCommand): void {
	switch (command.kind) {
		case "new-session":
			newSession();
			return;
		case "attach":
			selectRow(command.sessionId);
			return;
		case "open-decisions":
			openDecisions();
			return;
		case "run-doctor":
			runDoctor();
			return;
		case "show-stack":
			showRightPane("stack");
			return;
		case "open-settings":
			openSettings();
			return;
		case "toggle-theme":
			// "system" is a third state, not a third stop on the toggle: from
			// there the deliberate choice is dark, and the next press is light.
			setTheme(settings.theme === "dark" ? "light" : "dark");
			return;
	}
}

/** Run an entry and dismiss. The palette always closes — a command that
 *  no-ops (new session with no workspace) still ends the interaction. */
export function runPaletteEntry(entry: PaletteEntry): void {
	dispatch(entry.command);
	closePalette();
}

/** Enter. False when the query matched nothing, so the palette stays open. */
export function runPaletteSelection(): boolean {
	const entry = visibleEntries()[palette.index];
	if (entry === undefined) return false;
	runPaletteEntry(entry);
	return true;
}

/** Reset for tests. */
export function resetPalette(): void {
	palette.open = false;
	palette.query = "";
	palette.index = 0;
}
