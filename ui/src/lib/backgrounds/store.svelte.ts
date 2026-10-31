// The background preference for this window (TD-5202): which background,
// whether it moves, whether clicks ripple and how strongly the veil quiets
// it, plus the OS reduced-motion answer that overrides both toggles.
//
// It is read at import time, not on mount, so the first frame already
// paints the stored choice: someone who picked Off never sees Classic
// flash in while the app boots.

import { type BackgroundId } from "./catalog";
import {
	BACKGROUND_KEY,
	LAST_ON_KEY,
	MOTION_KEY,
	RIPPLES_KEY,
	STRENGTH_KEY,
	deviceStorage,
	flagValue,
	readLastOn,
	readPrefs,
	writeItem,
	type BackgroundLive,
	type BackgroundStrength,
} from "./prefs";

const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

function reduceQuery(win: Window | undefined): MediaQueryList | null {
	try {
		return win !== undefined && typeof win.matchMedia === "function" ? win.matchMedia(REDUCE_QUERY) : null;
	} catch {
		return null;
	}
}

function hostWindow(): Window | undefined {
	return typeof window === "undefined" ? undefined : window;
}

export const background: BackgroundLive = $state({
	...readPrefs(deviceStorage()),
	reducedMotion: reduceQuery(hostWindow())?.matches ?? false,
});

/** What the palette's toggle turns back on from Off. Kept here as well as
 *  stored, so the toggle works even where storage does not. */
let lastOn: BackgroundId = background.id !== "off" ? background.id : readLastOn(deviceStorage());

/** Re-read the stored choice (another window changed it, or a test reset it). */
export function loadBackground(): void {
	const store = deviceStorage();
	const prefs = readPrefs(store);
	background.id = prefs.id;
	background.motion = prefs.motion;
	background.ripples = prefs.ripples;
	background.strength = prefs.strength;
	lastOn = prefs.id !== "off" ? prefs.id : readLastOn(store);
}

export function setBackground(id: BackgroundId): void {
	background.id = id;
	const store = deviceStorage();
	writeItem(store, BACKGROUND_KEY, id);
	if (id !== "off") {
		lastOn = id;
		writeItem(store, LAST_ON_KEY, id);
	}
}

/** Off, or back on to the last background this device had (the palette's
 *  Toggle background). */
export function toggleBackground(): void {
	setBackground(background.id === "off" ? lastOn : "off");
}

export function setBackgroundMotion(on: boolean): void {
	background.motion = on;
	writeItem(deviceStorage(), MOTION_KEY, flagValue(on));
}

export function setBackgroundRipples(on: boolean): void {
	background.ripples = on;
	writeItem(deviceStorage(), RIPPLES_KEY, flagValue(on));
}

/** Subtle or Vivid veil behind the panes; Off and Classic have none to change. */
export function setBackgroundStrength(strength: BackgroundStrength): void {
	background.strength = strength;
	writeItem(deviceStorage(), STRENGTH_KEY, strength);
}

let starts = 0;
let stop: (() => void) | null = null;

/** Follow the OS reduced-motion setting and choices made in other windows of
 *  this app. Counted, so the layout and a test can both start it; the
 *  listeners go when the last caller stops. */
export function startBackground(win: Window | undefined = hostWindow()): () => void {
	starts += 1;
	if (starts === 1 && win !== undefined) {
		const query = reduceQuery(win);
		const onReduce = (): void => {
			background.reducedMotion = query?.matches ?? false;
		};
		const onStorage = (event: StorageEvent): void => {
			// Every background key starts with this one: the choice, Motion,
			// Click ripples, Strength and the last one on.
			if (event.key === null || event.key.startsWith(BACKGROUND_KEY)) loadBackground();
		};
		onReduce();
		query?.addEventListener("change", onReduce);
		win.addEventListener("storage", onStorage);
		stop = () => {
			query?.removeEventListener("change", onReduce);
			win.removeEventListener("storage", onStorage);
		};
	}
	let stopped = false;
	return () => {
		if (stopped) return;
		stopped = true;
		starts -= 1;
		if (starts === 0) {
			stop?.();
			stop = null;
		}
	};
}
