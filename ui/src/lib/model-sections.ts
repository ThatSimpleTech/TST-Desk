// TD-5236: the picker's main list in sections: Local, API, Grok, then any
// section a model names itself. The daemon says which section each model is
// in (`setup_state.preset_groups`) and the order of the built-in ones; this
// only arranges what it was sent.

export interface ModelSection {
	/** The section's heading, or "" when the list has a single section and
	 *  so needs none. */
	title: string;
	names: string[];
}

/** `names` in sections. Built-in sections come first, in the daemon's
 *  order; others follow by name. A model the daemon gave no section (an
 *  older daemon) goes in an untitled one at the top. One section in all
 *  means no headings: a short list stays the plain list it was. */
export function sectionModels(
	names: readonly string[],
	groups: Readonly<Record<string, string>>,
	order: readonly string[],
): ModelSection[] {
	const bySection = new Map<string, string[]>();
	for (const name of names) {
		const title = groups[name] ?? "";
		bySection.set(title, [...(bySection.get(title) ?? []), name]);
	}
	const rank = (title: string): number => {
		if (title === "") return -1;
		const at = order.indexOf(title);
		return at === -1 ? order.length : at;
	};
	const sections = [...bySection.entries()]
		.sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
		.map(([title, members]) => ({ title, names: members }));
	return sections.length === 1 ? [{ title: "", names: sections[0].names }] : sections;
}

/** The model in quick-switch slot `slot`, or null. */
export function modelAtSlot(
	slot: number,
	slots: Readonly<Record<string, number>>,
): string | null {
	return Object.keys(slots).find((name) => slots[name] === slot) ?? null;
}
