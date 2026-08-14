// Pure split-pane divider logic: clamping, unit conversion, persistence.
// DOM-free so it is unit-testable in the node vitest environment.

export const MIN_LEFT_PCT = 20;
export const MAX_LEFT_PCT = 80;
export const DEFAULT_LEFT_PCT = 40;
export const KEYBOARD_STEP_PCT = 2;
export const STORAGE_KEY = "tstd-desktop.splitpane.leftPct";

export function clampLeftPct(pct: number): number {
	if (!Number.isFinite(pct)) return DEFAULT_LEFT_PCT;
	return Math.min(MAX_LEFT_PCT, Math.max(MIN_LEFT_PCT, pct));
}

/** Convert a pixel offset within the container to a clamped left-pane
 *  percentage. Returns null when the container has no measurable width
 *  (e.g. not yet laid out), so callers can keep the current value. */
export function pxToLeftPct(px: number, containerWidth: number): number | null {
	if (containerWidth <= 0) return null;
	return clampLeftPct((px / containerWidth) * 100);
}

/** Minimal storage shape — matches the Web Storage API subset we use,
 *  so tests can substitute an in-memory fake. */
export interface KVStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
}

export function readPersistedLeftPct(storage: KVStorage): number {
	const raw = storage.getItem(STORAGE_KEY);
	if (raw === null || raw.trim() === "") return DEFAULT_LEFT_PCT;
	// Number() yields NaN for corrupt values; clampLeftPct normalizes those
	// to the default and out-of-range values to the bounds.
	return clampLeftPct(Number(raw));
}

export function writePersistedLeftPct(storage: KVStorage, pct: number): void {
	storage.setItem(STORAGE_KEY, String(clampLeftPct(pct)));
}
