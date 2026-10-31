// The background choice is per device (TD-5202), like the theme: it lives
// in this window's localStorage, not in the daemon's config, so a phone and
// a laptop attached to one daemon can each keep their own.
//
// Storage is allowed to fail. A private window, blocked site data or a
// webview with storage switched off can make the getter itself throw, so
// every touch goes through these helpers and a failure reads as "nothing
// stored" and falls back to the defaults below.

import { DEFAULT_BACKGROUND, isBackgroundId, type BackgroundId } from "./catalog";

export const BACKGROUND_KEY = "tst-desk:background";
export const MOTION_KEY = "tst-desk:background-motion";
export const RIPPLES_KEY = "tst-desk:background-ripples";
export const STRENGTH_KEY = "tst-desk:background-strength";
/** The last background other than Off, so the palette's toggle can bring it back. */
export const LAST_ON_KEY = "tst-desk:background-last";

/** How much the veil behind the panes quiets a background. Subtle is the
 *  veil as its token has it, strong enough that all text keeps AA; Vivid
 *  thins it so the background shows much more (registry.ts has by how much). */
export const STRENGTHS = ["subtle", "vivid"] as const;
export type BackgroundStrength = (typeof STRENGTHS)[number];

export function isStrength(value: unknown): value is BackgroundStrength {
	return (STRENGTHS as readonly unknown[]).includes(value);
}

export interface BackgroundPrefs {
	id: BackgroundId;
	/** Animated (true) or a still frame of the same background. */
	motion: boolean;
	/** A ring of light from every click in the chat. */
	ripples: boolean;
	strength: BackgroundStrength;
}

export const DEFAULT_PREFS: Readonly<BackgroundPrefs> = {
	id: DEFAULT_BACKGROUND,
	motion: true,
	ripples: true,
	strength: "subtle",
};

export type ReadStore = Pick<Storage, "getItem">;
export type WriteStore = Pick<Storage, "setItem">;

/** This window's localStorage, or null when there is none or touching it throws. */
export function deviceStorage(): Storage | null {
	try {
		return typeof localStorage === "undefined" ? null : localStorage;
	} catch {
		return null;
	}
}

function readItem(store: ReadStore | null, key: string): string | null {
	if (store === null) return null;
	try {
		return store.getItem(key);
	} catch {
		return null;
	}
}

/** Flags are stored as "on"/"off"; anything else keeps the default. */
function readFlag(store: ReadStore | null, key: string, fallback: boolean): boolean {
	const value = readItem(store, key);
	if (value === "on") return true;
	if (value === "off") return false;
	return fallback;
}

export function readPrefs(store: ReadStore | null): BackgroundPrefs {
	const id = readItem(store, BACKGROUND_KEY);
	const strength = readItem(store, STRENGTH_KEY);
	return {
		id: isBackgroundId(id) ? id : DEFAULT_PREFS.id,
		motion: readFlag(store, MOTION_KEY, DEFAULT_PREFS.motion),
		ripples: readFlag(store, RIPPLES_KEY, DEFAULT_PREFS.ripples),
		strength: isStrength(strength) ? strength : DEFAULT_PREFS.strength,
	};
}

/** The background to come back to from Off: the last one this device had
 *  on, or the default. */
export function readLastOn(store: ReadStore | null): BackgroundId {
	const id = readItem(store, LAST_ON_KEY);
	return isBackgroundId(id) && id !== "off" ? id : DEFAULT_PREFS.id;
}

/** Store one value. False when it could not be kept (no storage, quota,
 *  blocked); the choice still applies to this window until it closes. */
export function writeItem(store: WriteStore | null, key: string, value: string): boolean {
	if (store === null) return false;
	try {
		store.setItem(key, value);
		return true;
	} catch {
		return false;
	}
}

export function flagValue(on: boolean): "on" | "off" {
	return on ? "on" : "off";
}

/** What the window should actually do, once the OS has had its say. */
export interface BackgroundLive {
	id: BackgroundId;
	motion: boolean;
	ripples: boolean;
	strength: BackgroundStrength;
	/** The OS asks for reduced motion: still frame, no ripples, whatever the toggles say. */
	reducedMotion: boolean;
}

/** Whether frames run at all. Off never animates; reduced motion holds still. */
export function animates(state: BackgroundLive): boolean {
	return state.id !== "off" && state.motion && !state.reducedMotion;
}

/** Whether a click lights a ripple. Off has none; ripples are motion, so
 *  reduced motion turns them off too. */
export function ripplesLive(state: BackgroundLive): boolean {
	return state.id !== "off" && state.ripples && !state.reducedMotion;
}
