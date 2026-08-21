// Command palette registry and matching (TD-1707).
//
// Pure and rune-free so the action list and the matcher are unit-testable in
// the node vitest environment; palette-store.svelte.ts appends the live
// sessions, holds the open/query/selection state, and runs the commands.
//
// An entry names its glyph as an `IconName`, never as a free string: the
// shared map (icons.ts) is the only source of glyphs, and a typo that slipped
// through would ship as a blank square rather than an error.

import type { IconName } from "./icons";

/** What running an entry does. The store owns the dispatch; this is the name. */
export type PaletteCommand =
	| { kind: "new-session" }
	| { kind: "attach"; sessionId: string }
	| { kind: "open-decisions" }
	| { kind: "run-doctor" }
	| { kind: "show-stack" }
	| { kind: "show-work" }
	| { kind: "open-settings" }
	| { kind: "toggle-theme" }
	| { kind: "end-session" };

export interface PaletteEntry {
	/** Stable key for the rendered list. */
	id: string;
	title: string;
	/** The second line. Searched too, so "ledger" finds the decisions entry. */
	subtitle: string;
	icon: IconName;
	/** Words worth matching that neither visible line says. */
	keywords: string;
	command: PaletteCommand;
}

/** The fixed actions, in the order they show with an empty query. Sessions are
 *  appended by the store — they come from the daemon and change under us. */
export const ACTION_ENTRIES: readonly PaletteEntry[] = [
	{
		id: "action:new-session",
		title: "New session",
		subtitle: "Start a fresh session in this workspace",
		icon: "plus",
		keywords: "create start chat",
		command: { kind: "new-session" },
	},
	{
		id: "action:open-decisions",
		title: "Open decisions",
		subtitle: "This session's decision ledger",
		icon: "scroll",
		keywords: "ledger revert undo class",
		command: { kind: "open-decisions" },
	},
	{
		id: "action:run-doctor",
		title: "Run doctor",
		subtitle: "Check the daemon, the key, and the provider",
		icon: "stethoscope",
		keywords: "diagnostics health troubleshoot",
		command: { kind: "run-doctor" },
	},
	{
		id: "action:show-stack",
		title: "Open stack",
		subtitle: "The resolved instruction stack",
		icon: "layers",
		keywords: "steering context rules panel",
		command: { kind: "show-stack" },
	},
	{
		id: "action:show-work",
		title: "Open work",
		subtitle: "Session diffs as a reviewable stack",
		icon: "file",
		keywords: "diffs writes files review pane",
		command: { kind: "show-work" },
	},
	{
		id: "action:open-settings",
		title: "Open settings",
		subtitle: "Appearance, model, policy, API key",
		icon: "settings",
		keywords: "preferences config theme",
		command: { kind: "open-settings" },
	},
	{
		id: "action:toggle-theme",
		title: "Toggle theme",
		subtitle: "Switch between light and dark",
		icon: "moon",
		keywords: "appearance dark light",
		command: { kind: "toggle-theme" },
	},
	{
		id: "action:end-session",
		title: "End session",
		subtitle: "Distill this session into memory",
		icon: "check",
		keywords: "distill quit memory close",
		command: { kind: "end-session" },
	},
];

// ── Matching ──────────────────────────────────────────────────────────
//
// Subsequence matching, greedy left to right: every character of the query
// has to appear in order, and where they land decides the rank. Greedy is
// not optimal placement, but the list is a dozen entries and a session or
// two — an optimal matcher would cost more to read than it buys.

/** The character class that precedes a word start. */
const BOUNDARY = /[^a-z0-9]/;

/** Runs of adjacent characters are what "typing a prefix" looks like. */
const CONSECUTIVE_BONUS = 6;
/** Hitting the front of a word beats hitting its middle. */
const BOUNDARY_BONUS = 10;
/** Skipping is penalized, but a huge skip is no worse than a big one —
 *  otherwise one late character sinks an otherwise good match. */
const MAX_GAP_PENALTY = 8;
/** A title hit always outranks a hit in the supporting text. */
const TITLE_BONUS = 40;

/** Score `text` against `query`, or null when the query isn't a subsequence.
 *  Higher is better; an empty query matches everything at zero. */
export function fuzzyScore(query: string, text: string): number | null {
	const needle = query.toLowerCase();
	const hay = text.toLowerCase();
	if (needle === "") return 0;
	let score = 0;
	let from = 0;
	let previous = -2;
	for (const ch of needle) {
		const at = hay.indexOf(ch, from);
		if (at < 0) return null;
		if (at === previous + 1) score += CONSECUTIVE_BONUS;
		if (at === 0 || BOUNDARY.test(hay[at - 1])) score += BOUNDARY_BONUS;
		score -= Math.min(at - from, MAX_GAP_PENALTY);
		previous = at;
		from = at + 1;
	}
	return score;
}

/** An entry's best score: its title, else its subtitle and keywords. */
export function matchScore(query: string, entry: PaletteEntry): number | null {
	const title = fuzzyScore(query, entry.title);
	if (title !== null) return title + TITLE_BONUS;
	return fuzzyScore(query, `${entry.subtitle} ${entry.keywords}`);
}

/** The matching entries, best first. Ties keep the registry's own order, so
 *  an empty or weak query still reads as a stable menu rather than a shuffle. */
export function rankEntries(
	entries: readonly PaletteEntry[],
	query: string,
): PaletteEntry[] {
	const needle = query.trim();
	if (needle === "") return [...entries];
	const scored: { entry: PaletteEntry; order: number; score: number }[] = [];
	entries.forEach((entry, order) => {
		const score = matchScore(needle, entry);
		if (score !== null) scored.push({ entry, order, score });
	});
	scored.sort((a, b) => b.score - a.score || a.order - b.order);
	return scored.map((s) => s.entry);
}

/** Where ↑/↓ lands. Wraps both ways; an empty list stays put at 0. */
export function nextIndex(current: number, delta: number, count: number): number {
	if (count <= 0) return 0;
	return (((current + delta) % count) + count) % count;
}
