// Pure split-pane divider logic: clamping, persistence. DOM-free so it is
// unit-testable in the node vitest environment.
//
// The right pane (Activity / inspector) is a pixel-width sidebar. The chat
// pane takes the leftover. A percentage split (20–80%) could not be dragged
// to an arbitrary size; pixels can, clamped only so neither pane disappears.

export const MIN_RIGHT_PX = 200;
/** Chat keeps at least this much of the split container. */
export const MIN_LEFT_PX = 280;
export const DEFAULT_RIGHT_PX = 320;
export const KEYBOARD_STEP_PX = 16;
export const STORAGE_KEY = "tstd-desktop.splitpane.rightPx";
/** Minimum pointer target for the divider (TD-1011). The painted rule
 *  stays `--space-1` (4px); the hit area is this wide. */
export const DIVIDER_HIT_MIN_PX = 8;

/** Widest the inspector may be in a container of this width. */
export function maxRightPx(containerWidth: number): number {
	if (!(containerWidth > 0)) return MIN_RIGHT_PX;
	return Math.max(MIN_RIGHT_PX, containerWidth - MIN_LEFT_PX);
}

export function clampRightPx(px: number, containerWidth?: number): number {
	if (!Number.isFinite(px)) return DEFAULT_RIGHT_PX;
	if (containerWidth === undefined || containerWidth <= 0) {
		return Math.max(MIN_RIGHT_PX, px);
	}
	return Math.min(maxRightPx(containerWidth), Math.max(MIN_RIGHT_PX, px));
}

/** Convert a pointer x-coordinate into a clamped right-pane width.
 *  Returns null when the container has no measurable width (e.g. not yet
 *  laid out), so callers can keep the current value. */
export function pointerToRightPx(
	clientX: number,
	containerLeft: number,
	containerWidth: number,
): number | null {
	if (containerWidth <= 0) return null;
	return clampRightPx(containerLeft + containerWidth - clientX, containerWidth);
}

/** Minimal storage shape — matches the Web Storage API subset we use,
 *  so tests can substitute an in-memory fake. */
export interface KVStorage {
	getItem(key: string): string | null;
	setItem(key: string, value: string): void;
}

export function readPersistedRightPx(storage: KVStorage): number {
	const raw = storage.getItem(STORAGE_KEY);
	if (raw === null || raw.trim() === "") return DEFAULT_RIGHT_PX;
	// Number() yields NaN for corrupt values; clampRightPx normalizes those
	// to the default. Out-of-range values keep a floor of MIN_RIGHT_PX and
	// are capped against the container later, when it is known.
	return clampRightPx(Number(raw));
}

export function writePersistedRightPx(storage: KVStorage, px: number): void {
	storage.setItem(STORAGE_KEY, String(clampRightPx(px)));
}

/** The subset of `EventTarget` a drag needs — window in the app, a fake
 *  in tests. Capture is an optimisation; these listeners are the
 *  mechanism (TD-1011). */
export interface DragTarget {
	addEventListener(type: "pointermove" | "pointerup" | "pointercancel", listener: (e: PointerEvent) => void): void;
	removeEventListener(type: "pointermove" | "pointerup" | "pointercancel", listener: (e: PointerEvent) => void): void;
}

/** Attach move/up to `target` for the duration of a drag. Returns the
 *  detach function; call it from the up handler (and from unmount). */
export function attachDragListeners(
	target: DragTarget,
	move: (e: PointerEvent) => void,
	up: (e: PointerEvent) => void,
): () => void {
	target.addEventListener("pointermove", move);
	target.addEventListener("pointerup", up);
	target.addEventListener("pointercancel", up);
	return () => {
		target.removeEventListener("pointermove", move);
		target.removeEventListener("pointerup", up);
		target.removeEventListener("pointercancel", up);
	};
}
