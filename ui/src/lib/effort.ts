// Model picker: the effort levels, the model grouping, and the chip text.
//
// The picker works like Claude's: a short model list, an Effort submenu,
// "More models" for the raw routes, and "Teams" (a model per role) when the
// config has one. Fast and Ultracode are switches beside Effort, not levels
// (TD-5176, TD-5195). Every listed entry has a label and, ideally, a one-line
// description (TD-5172). Pure functions here so vitest can drive
// them; the popover is components/chat/ModelPicker.svelte.

import type { EffortLevel } from "./protocol";

export const DEFAULT_EFFORT: EffortLevel = "medium";

/** The old sixth level (TD-5195). An older peer may still report it; it
 *  means Max with the Ultracode switch on. Never sent. */
export const LEGACY_ULTRACODE = "ultracode";

/** The submenu's hint line. */
export const EFFORT_HINT = "Higher effort means more thorough responses, but takes longer.";

export interface EffortOption {
	/** The wire value sent as `reasoning_effort`. */
	id: EffortLevel;
	/** What the menu and the chip say. */
	label: string;
	/** A positive badge beside the label. */
	badge?: string;
	/** A warning badge beside the label. */
	warning?: string;
}

export const EFFORT_OPTIONS: readonly EffortOption[] = [
	{ id: "low", label: "Low" },
	{ id: "medium", label: "Medium", badge: "Recommended" },
	{ id: "high", label: "High" },
	{ id: "xhigh", label: "Extra" },
	{ id: "max", label: "Max", warning: "slower, uses the big model" },
];

export function effortLabel(effort: EffortLevel): string {
	return EFFORT_OPTIONS.find((option) => option.id === effort)?.label ?? "Medium";
}

/** What the list and chip call a preset: its configured `label`, else its key. */
export function modelLabel(preset: string, labels: Readonly<Record<string, string>> = {}): string {
	return labels[preset] ?? preset;
}

export interface ModelGroups {
	main: string[];
	more: string[];
	/** Per-role presets (`picker: team`); the "Teams" submenu shows only when
	 *  this is non-empty. */
	teams: string[];
}

export interface GroupOptions {
	/** Presets under "More models" (config `picker: more`). */
	more?: readonly string[];
	/** Presets under "Teams" (config `picker: team`). */
	teams?: readonly string[];
	/** Display names (config `label`). A preset with none is hidden, except
	 *  the current one (TD-5172): every listed item has a name a person set. */
	labels?: Readonly<Record<string, string>>;
	/** The session's preset; always listed so the check mark has a home. */
	current?: string;
}

/** Split the declared presets into the main list, "More models" and "Teams".
 *  Unlabelled presets are left out unless current; the current preset stays
 *  findable even when the daemon did not list it. */
export function groupModels(presets: readonly string[], options: GroupOptions = {}): ModelGroups {
	const { more = [], teams = [], labels, current = "" } = options;
	const all = current !== "" && !presets.includes(current) ? [current, ...presets] : presets;
	const names =
		labels === undefined ? all : all.filter((name) => name === current || name in labels);
	return {
		main: names.filter((name) => !more.includes(name) && !teams.includes(name)),
		more: names.filter((name) => more.includes(name) && !teams.includes(name)),
		teams: names.filter((name) => teams.includes(name)),
	};
}

/** The one-line subtitle under a model's name, or "" for none. */
export function modelDescription(
	preset: string,
	descriptions: Readonly<Record<string, string>> = {},
): string {
	return descriptions[preset] ?? "";
}

/** The chip under the composer: `House Medium`. */
export function chipLabel(
	preset: string,
	effort: EffortLevel,
	labels: Readonly<Record<string, string>> = {},
): string {
	const model = modelLabel(preset, labels);
	const level = effortLabel(effort);
	return model === "" ? level : `${model} ${level}`;
}
