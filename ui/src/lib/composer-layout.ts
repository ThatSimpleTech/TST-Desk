// Composer model labels.
//
// The dropdown shows the role and the model that role is running:
// `brain (moonshotai/kimi-k3)`. No parentheses until a model exists.

/** Vertical padding the composer textarea adds around one line of text. */
export const COMPOSER_LINE_PADDING = 16;

/** `brain (moonshotai/kimi-k3)`, or just the role when no model is known.
 *  A tier on a backup endpoint this turn (TD-5120) says so:
 *  `brain (gpt-x, backup 1)`; *slug* is then the backup's. */
export function tierMenuLabel(
	tier: string,
	slug: string | null | undefined,
	backup: number | undefined = undefined,
): string {
	const model = (slug ?? "").trim();
	const onBackup = backup !== undefined && backup > 0 ? `backup ${backup}` : "";
	const inside = [model, onBackup].filter((part) => part !== "").join(", ");
	return inside === "" ? tier : `${tier} (${inside})`;
}
