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
export type RailSurface = "home" | "projects" | "artifacts" | "scheduled";

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

/** The function entries for a given current surface, in rail order.
 *
 * Home, Projects, and Artifacts trade `current` / `ready` so the rail is
 * honest about which pane the window is showing (TD-2801 / TD-3202).
 * Scheduled belongs to v0.5 and stays `planned`: hiding it would hide
 * the shape of the app.
 */
export function railFunctions(current: RailSurface = "home"): RailEntry[] {
	return [
		{
			id: "home",
			label: "Home",
			icon: "home",
			state: current === "home" ? "current" : "ready",
			note: null,
		},
		{
			id: "projects",
			label: "Projects",
			icon: "folder",
			state: current === "projects" ? "current" : "ready",
			note: null,
		},
		{
			id: "artifacts",
			label: "Artifacts",
			icon: "box",
			state: current === "artifacts" ? "current" : "ready",
			note: null,
		},
		{ id: "scheduled", label: "Scheduled", icon: "clock", state: "planned", note: "v0.5" },
	];
}

/** Snapshot of the home-surface registry. Prefer `railFunctions(current)`
 *  when the window's surface can move. */
export const RAIL_FUNCTIONS: readonly RailEntry[] = railFunctions("home");

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
 *  sectioned below. `historyCount` is the number of rows the rail will list.
 *
 *  `archivedView` swaps the history section to the archived shelf rather than
 *  adding a second section: archived sessions are the same rows filed
 *  elsewhere, and two live lists in a 260px column would compete for the same
 *  scroll. The heading is the only thing that tells you which you are in, so
 *  it has to change (TD-1715). */
export function railSections(
	historyCount: number,
	archivedView = false,
	current: RailSurface = "home",
): RailSection[] {
	return [
		{
			id: "functions",
			label: "Surfaces",
			heading: false,
			badge: null,
			entries: railFunctions(current),
		},
		{
			id: "history",
			label: archivedView ? "Archived" : "History",
			heading: true,
			badge: historyBadge(historyCount),
			entries: [],
		},
	];
}

// ── Row lifecycle actions (TD-1715) ────────────────────────────────────

export type RailRowActionId = "star" | "unstar" | "archive" | "unarchive" | "move" | "delete";

export interface RailRowAction {
	id: RailRowActionId;
	label: string;
	icon: IconName;
	/** Irreversible: rendered in the danger tone and confirmed before firing. */
	danger: boolean;
	/** Hover/assistive text — says what the action costs, not what it is. */
	hint: string;
}

/** Copy for Move, stated wherever Move is offered.
 *
 *  The backlog is explicit that this must say so plainly: reassigning the
 *  workspace moves the agent's working context, not just a label in a list. */
export const MOVE_HINT =
	"Reassign this session to another project. The agent's working directory " +
	"and boundary root change on the next turn; the conversation moves with it.";

/** Copy for Delete's confirm step.
 *
 *  Names exactly what is destroyed and what is not, because "delete" in a
 *  session list could plausibly mean either. */
export const DELETE_CONFIRM =
	"Delete this session and its event log? The conversation can't be " +
	"recovered. Archive instead to hide it and keep it.";

/** The actions a row offers, given whether it is filed away.
 *
 *  Archive and Unarchive are one slot, never both: a row is in exactly one of
 *  the two shelves, so offering the other is offering a no-op. Whether Delete
 *  and Move can actually run is the daemon's call — it alone knows if a turn
 *  is in flight — so they are always offered and the refusal comes back typed
 *  (§6: the UI never derives truth it wasn't given). */
export function rowActions(archived: boolean, starred = false): RailRowAction[] {
	return [
		starred
			? {
					id: "unstar",
					label: "Unstar",
					icon: "star",
					danger: false,
					hint: "Remove this session from the top of the list.",
				}
			: {
					id: "star",
					label: "Star",
					icon: "star",
					danger: false,
					hint: "Keep this session at the top of the list.",
				},
		archived
			? {
					id: "unarchive",
					label: "Unarchive",
					icon: "archive",
					danger: false,
					hint: "Restore this session to the session list.",
				}
			: {
					id: "archive",
					label: "Archive",
					icon: "archive",
					danger: false,
					hint: "Hide this session from the list. Keeps everything, cancels nothing.",
				},
		{ id: "move", label: "Move to project", icon: "folder", danger: false, hint: MOVE_HINT },
		{
			id: "delete",
			label: "Delete",
			icon: "trash",
			danger: true,
			hint: "Delete this session and its event log. Can't be undone.",
		},
	];
}

/** The shelf toggle's label and assistive text: it names where the click
 *  goes, never where you already are. */
export function archivedToggle(archivedView: boolean): { label: string; hint: string } {
	return archivedView
		? { label: "Sessions", hint: "Back to the session list" }
		: { label: "Archived", hint: "Show archived sessions" };
}

export function starredToggle(starredOnly: boolean): { label: string; hint: string } {
	return starredOnly
		? { label: "All", hint: "Show every session on this shelf" }
		: { label: "Starred", hint: "Show starred sessions only" };
}

/** Empty-state copy per shelf and filter — four different situations that
 *  must not share one sentence. */
export function emptyRowsCopy(
	archivedView: boolean,
	filtered: boolean,
	anyRows: boolean,
	starredOnly = false,
): string {
	if (filtered && anyRows) return "No matching sessions";
	if (starredOnly) return "No starred sessions";
	if (archivedView) return "No archived sessions";
	return "No sessions yet";
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
