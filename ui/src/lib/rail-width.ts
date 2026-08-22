// Pure session-rail resize logic: clamping, persistence. DOM-free so it is
// unit-testable in the node vitest environment. The rail is a fixed-position
// column (unlike the percentage-based SplitPane), so the unit is pixels.
import type { KVStorage } from "./splitpane";

export const MIN_RAIL_PX = 200;
export const MAX_RAIL_PX = 440;
/** Matches the rail's CSS default (`flex: 0 0 260px`). */
export const DEFAULT_RAIL_PX = 260;
export const RAIL_KEYBOARD_STEP_PX = 16;
export const RAIL_STORAGE_KEY = "tstd-desktop.sessionRail.widthPx";

export function clampRailPx(px: number): number {
	if (!Number.isFinite(px)) return DEFAULT_RAIL_PX;
	return Math.min(MAX_RAIL_PX, Math.max(MIN_RAIL_PX, px));
}

export function readPersistedRailPx(storage: KVStorage): number {
	const raw = storage.getItem(RAIL_STORAGE_KEY);
	if (raw === null || raw.trim() === "") return DEFAULT_RAIL_PX;
	// Number() yields NaN for corrupt values; clampRailPx normalizes those
	// to the default and out-of-range values to the bounds.
	return clampRailPx(Number(raw));
}

export function writePersistedRailPx(storage: KVStorage, px: number): void {
	storage.setItem(RAIL_STORAGE_KEY, String(clampRailPx(px)));
}
