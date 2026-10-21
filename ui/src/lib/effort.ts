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

/** `ezer` reads as "Ezer"; any other preset keeps the name it was given. */
export function modelLabel(preset: string): string {
	return preset === "ezer" ? "Ezer" : preset;
}

/** `sub-claude-opus`, `ezer-forge` and the other raw routes live under
 *  "More models"; everything else, `ezer` included, is on the main list. */
export function isMoreModel(preset: string): boolean {
	return preset.startsWith("sub-") || preset.startsWith("ezer-");
}

export interface ModelGroups {
	main: string[];
	more: string[];
}

/** Split the declared presets into the main list and "More models". The
 *  current preset stays findable even when the daemon did not list it. */
export function groupModels(presets: readonly string[], current = ""): ModelGroups {
	const names = current !== "" && !presets.includes(current) ? [current, ...presets] : presets;
	return {
		main: names.filter((name) => !isMoreModel(name)),
		more: names.filter(isMoreModel),
	};
}

/** The chip under the composer: `Ezer Medium`. */
export function chipLabel(preset: string, effort: EffortLevel): string {
	const model = modelLabel(preset);
	const level = effortLabel(effort);
	return model === "" ? level : `${model} ${level}`;
}
