// Composer model labels and the keyboard's lift.
//
// The dropdown shows the role and the model that role is running:
// `brain (moonshotai/kimi-k3)`. No parentheses until a model exists.

/** Vertical padding the composer textarea adds around one line of text. */
export const COMPOSER_LINE_PADDING = 16;

/** The keyboard's height in CSS pixels: the layout viewport minus the
 *  visual viewport. A pinch-zoomed visual viewport reports no keyboard inset,
 *  so it never becomes padding. Returns 0 when the keyboard is down. */
export function keyboardInsetPx(layoutHeight: number, visualHeight: number, scale = 1): number {
	if (layoutHeight <= 0 || visualHeight <= 0 || Math.abs(scale - 1) > 0.01) return 0;
	return Math.max(0, Math.round(layoutHeight - visualHeight));
}

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
