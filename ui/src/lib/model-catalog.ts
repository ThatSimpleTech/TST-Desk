// TD-5237: Settings → Model lists every model in the catalog with Duplicate,
// Rename and Remove. The daemon does the work; this is the one thing the
// window decides by itself, the label a copy starts with.

/** Longest label the daemon accepts (`Preset.label`). */
export const LABEL_MAX = 40;

/** A label for a copy of `label` that no other model has: "X copy", then
 *  "X copy 2", and so on. The base is cut so the result fits LABEL_MAX. */
export function copyLabel(label: string, taken: readonly string[]): string {
	const used = new Set(taken.map((name) => name.trim().toLowerCase()));
	for (let n = 1; ; n += 1) {
		const suffix = n === 1 ? " copy" : ` copy ${n}`;
		const candidate = label.trim().slice(0, LABEL_MAX - suffix.length).trimEnd() + suffix;
		if (!used.has(candidate.toLowerCase())) return candidate;
	}
}

/** What a typed label would be saved as, or null when it cannot be saved:
 *  empty, too long, or the label the model already has. */
export function renameTarget(draft: string, current: string): string | null {
	const next = draft.trim();
	if (next === "" || next.length > LABEL_MAX || next === current) return null;
	return next;
}
