// Rail information architecture (TD-1712).
//
// The rail's *layout grammar*, kept apart from the rail's data: which
// function surfaces sit above the session history, which of them their epic
// has actually landed, how the history section badges its count, and what the
// bottom-left account row reads. Pure and rune-free so every rule here is
// unit-testable without rendering a component.
//
// The registry is the single place that answers "can this row be clicked, and
// if not, why not?". Only a `ready` surface activates; the other two render
// non-interactive, and `activateRailFunction` (sessions.svelte.ts) refuses
// them at the store as well. Never a dead click, from either direction.

import type { IconName } from "./icons";

/** The function surfaces the rail offers above session history. */
export type RailSurface = "home" | "projects" | "scheduled";

/** Why a row is, or isn't, a place to go.
 *
 * Three states, not a landed/unlanded boolean, because the two reasons a row
 * can't be clicked are different claims and must not read alike: `current` is
 * the surface the window already shows, `planned` is a surface whose epic
 * hasn't landed. Only `ready` navigates. */
export type RailEntryState = "current" | "ready" | "planned";

export interface RailEntry {
	id: RailSurface;
	label: string;
	icon: IconName;
	state: RailEntryState;
	/** Milestone a `planned` surface arrives in. Null for the other states. */
	note: string | null;
}

/** The function entries, in rail order.
 *
 * Home is `current`: the window has one main surface and the chat pane is it,
 * so Home is where you already are, not somewhere to navigate — it renders
 * selected rather than as a button that would do nothing. Projects is
 * TD-1103's recents quick-switch. Scheduled belongs to v0.5 and renders
 * disabled with its milestone: hiding it would hide the shape of the app.
 */
export const RAIL_FUNCTIONS: readonly RailEntry[] = [
	{ id: "home", label: "Home", icon: "home", state: "current", note: null },
	{ id: "projects", label: "Projects", icon: "folder", state: "ready", note: null },
	{ id: "scheduled", label: "Scheduled", icon: "clock", state: "planned", note: "v0.5" },
] as const;

export type RailSectionId = "functions" | "history";

export interface RailSection {
	id: RailSectionId;
	/** The group's accessible name; rendered visibly only when `heading`. */
	label: string;
	/** True when the group carries a visible heading. The function group reads
	 *  as chrome and carries none, matching the reference rail. */
	heading: boolean;
	/** Count badge beside the heading; null when there is nothing to badge. */
	badge: string | null;
	/** Function entries, in rail order. Empty for history — its rows come from
	 *  the store's `visibleRows()`, so the filter keeps one code path. */
	entries: readonly RailEntry[];
}

/** History's count badge: the number of rows on show, null when none.
 *
 * The count is what the section actually lists (post-filter), so the badge can
 * never disagree with the rows under it. Three digits would widen the rail, so
 * it saturates. */
export function historyBadge(count: number): string | null {
	if (count <= 0) return null;
	return count > 99 ? "99+" : String(count);
}

/** The rail top to bottom: function entries grouped above, session history
 *  sectioned below. `historyCount` is the number of rows the rail will list. */
export function railSections(historyCount: number): RailSection[] {
	return [
		{
			id: "functions",
			label: "Surfaces",
			heading: false,
			badge: null,
			entries: RAIL_FUNCTIONS,
		},
		{
			id: "history",
			label: "History",
			heading: true,
			badge: historyBadge(historyCount),
			entries: [],
		},
	];
}

/** Hover/assistive text for a function entry: each non-clickable row says why
 *  it isn't, rather than looking like it failed. */
export function entryHint(entry: RailEntry): string {
	if (entry.state === "current") return `${entry.label} — you are here`;
	if (entry.state === "planned") return `${entry.label} — arrives in ${entry.note}`;
	return entry.label;
}

export interface AccountRow {
	/** Uppercase initial for the avatar slot; null when there's no label to
	 *  take one from, and the row falls back to the person glyph. */
	initial: string | null;
	label: string;
	/** Key *presence* from the daemon's setup_state — never the key (§2.2). */
	note: string;
}

/** The bottom-left account row: who the app is running as.
 *
 * TST Desk has no accounts (§2) — the identity is the active provider preset
 * and whether a key is stored, both of them daemon truth from `setup_state`.
 * The preset name shows verbatim; capitalizing it would invent a name the
 * config never used. */
export function accountRow(activePreset: string | null, hasApiKey: boolean): AccountRow {
	const label = activePreset ?? "No provider";
	const first = activePreset?.match(/[a-z0-9]/i)?.[0] ?? null;
	return {
		initial: first === null ? null : first.toUpperCase(),
		label,
		note: hasApiKey ? "Key stored" : "No key",
	};
}
