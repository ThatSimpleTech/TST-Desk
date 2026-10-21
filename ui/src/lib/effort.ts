// Model picker: the effort levels, the model grouping, and the chip text.
//
// The picker works like Claude's: a short model list, an Effort submenu,
// "More models" for the raw routes. Pure functions here so vitest can drive
// them; the popover is components/chat/ModelPicker.svelte.

import type { EffortLevel } from "./protocol";

export const DEFAULT_EFFORT: EffortLevel = "medium";

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
	/** Drawn in the accent colour, at the bottom. */
	accent?: boolean;
}

export const EFFORT_OPTIONS: readonly EffortOption[] = [
	{ id: "low", label: "Low" },
	{ id: "medium", label: "Medium", badge: "Recommended" },
	{ id: "high", label: "High" },
	{ id: "xhigh", label: "Extra" },
	{ id: "max", label: "Max", warning: "slower, uses the big model" },
	{ id: "ultracode", label: "Ultracode", accent: true },
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
}

/** Split the declared presets into the main list and "More models" (the
 *  config's `picker: more`). The current preset stays findable even when the
 *  daemon did not list it. */
export function groupModels(
	presets: readonly string[],
	morePresets: readonly string[] = [],
	current = "",
): ModelGroups {
	const names = current !== "" && !presets.includes(current) ? [current, ...presets] : presets;
	return {
		main: names.filter((name) => !morePresets.includes(name)),
		more: names.filter((name) => morePresets.includes(name)),
	};
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
