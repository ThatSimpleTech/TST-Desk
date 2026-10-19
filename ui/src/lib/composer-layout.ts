// Composer model labels.
//
// The dropdown shows the role and the model that role is running:
// `brain (moonshotai/kimi-k3)`. No parentheses until a model exists.

/** Vertical padding the composer textarea adds around one line of text. */
export const COMPOSER_LINE_PADDING = 16;

/** `brain (moonshotai/kimi-k3)`, or just the role when no model is known. */
export function tierMenuLabel(tier: string, slug: string | null | undefined): string {
	const model = (slug ?? "").trim();
	if (model === "") return tier;
	return `${tier} (${model})`;
}
