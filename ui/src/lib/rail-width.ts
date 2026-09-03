// Pure session-rail resize logic: clamping, persistence. DOM-free so it is
// unit-testable in the node vitest environment. The rail is a fixed-position
// column (unlike the leftover-width SplitPane), so the unit is pixels.
import { MIN_LEFT_PX, MIN_RIGHT_PX, type KVStorage } from "./splitpane";

export const MIN_RAIL_PX = 200;
/** Matches the rail's CSS default (`flex: 0 0 260px`). */
export const DEFAULT_RAIL_PX = 260;
export const RAIL_KEYBOARD_STEP_PX = 16;
export const RAIL_STORAGE_KEY = "tstd-desktop.sessionRail.widthPx";

/** Widest the rail may be in a viewport of this width, leaving the chat
 *  and inspector their split-pane minima. */
export function railMaxPx(viewportWidth: number): number {
	if (!(viewportWidth > 0)) return MIN_RAIL_PX;
	return Math.max(MIN_RAIL_PX, viewportWidth - MIN_LEFT_PX - MIN_RIGHT_PX);
}

export function clampRailPx(px: number, maxPx?: number): number {
	if (!Number.isFinite(px)) return DEFAULT_RAIL_PX;
	const max =
		maxPx !== undefined && Number.isFinite(maxPx) ? Math.max(MIN_RAIL_PX, maxPx) : Number.POSITIVE_INFINITY;
	return Math.min(max, Math.max(MIN_RAIL_PX, px));
}

export function readPersistedRailPx(storage: KVStorage): number {
	const raw = storage.getItem(RAIL_STORAGE_KEY);
	if (raw === null || raw.trim() === "") return DEFAULT_RAIL_PX;
	// Number() yields NaN for corrupt values; clampRailPx normalizes those
	// to the default. A huge stored width is kept — the layout path caps it
	// against the live viewport so a wide preference survives a shrink.
	return clampRailPx(Number(raw));
}

export function writePersistedRailPx(storage: KVStorage, px: number): void {
	storage.setItem(RAIL_STORAGE_KEY, String(clampRailPx(px)));
}
