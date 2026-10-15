// Where the composer's model controls sit.
//
// A one-line draft keeps them on the same row, to the right of Send. A draft
// that would wrap in that leftover width drops them to a footer so the text
// keeps the full row. The width used for that check is always the inline
// leftover — measuring the textarea after the controls have already moved
// would flip a borderline line back and forth.

/** Vertical padding the composer textarea adds around one line of text. */
export const COMPOSER_LINE_PADDING = 16;

/**
 * Dropdown label for a tier: the role, then the model that tier is actually
 * running. `brain (moonshotai/kimi-k3)`. No parentheses until a model exists.
 */
export function tierMenuLabel(tier: string, slug: string | null | undefined): string {
	const model = (slug ?? "").trim();
	if (model === "") return tier;
	return `${tier} (${model})`;
}

/** Width the draft has while the model controls share its row. */
export function inlineDraftWidth(
	entryWidth: number,
	actionsWidth: number,
	modelWidth: number,
	gap: number,
): number {
	if (!(entryWidth > 0)) return 0;
	return Math.max(0, entryWidth - actionsWidth - modelWidth - gap * 2);
}

/**
 * True when the model controls should leave the send row.
 *
 * A newline is always more than one line. A row that has been laid out
 * (`entryWidth`) but has no room left beside the controls is a footer too —
 * otherwise the chips would cover the draft. An unmeasured box (no entry
 * width yet) must not count as wrapped.
 */
export function modelControlsInFooter(
	text: string,
	measureHeight: number,
	lineHeight: number,
	measureWidth: number,
	entryWidth = 0,
): boolean {
	if (text.includes("\n")) return true;
	if (!(lineHeight > 0)) return false;
	if (entryWidth > 0 && measureWidth <= 0) return true;
	if (!(measureWidth > 0)) return false;
	return measureHeight > lineHeight + 1;
}
