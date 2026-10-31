// @vitest-environment jsdom
//
// The background preference store (TD-5202) against a real (jsdom)
// localStorage: the default, persistence, a bad stored value, storage that
// throws, a choice made in another window, and the OS reduced-motion
// setting following live. Strength goes the same way as the switches.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	background,
	loadBackground,
	setBackground,
	setBackgroundMotion,
	setBackgroundRipples,
	setBackgroundStrength,
	startBackground,
	toggleBackground,
} from "./store.svelte.js";
import { BACKGROUND_KEY, LAST_ON_KEY, MOTION_KEY, RIPPLES_KEY, STRENGTH_KEY } from "./prefs";

let stops: (() => void)[] = [];

beforeEach(() => {
	localStorage.clear();
	loadBackground();
	background.reducedMotion = false;
});

afterEach(() => {
	for (const stop of stops) stop();
	stops = [];
	vi.restoreAllMocks();
	localStorage.clear();
});

describe("the stored choice", () => {
	it("defaults to Classic, moving, with ripples", () => {
		expect(background).toMatchObject({ id: "classic", motion: true, ripples: true, strength: "subtle" });
	});

	it("persists each choice under its own key and reads it back", () => {
		setBackground("galaxy");
		setBackgroundMotion(false);
		setBackgroundRipples(false);
		setBackgroundStrength("vivid");
		expect(background.strength).toBe("vivid");
		expect(localStorage.getItem(BACKGROUND_KEY)).toBe("galaxy");
		expect(localStorage.getItem(MOTION_KEY)).toBe("off");
		expect(localStorage.getItem(RIPPLES_KEY)).toBe("off");
		expect(localStorage.getItem(STRENGTH_KEY)).toBe("vivid");
		background.id = "classic";
		background.motion = true;
		background.ripples = true;
		background.strength = "subtle";
		loadBackground();
		expect(background).toMatchObject({ id: "galaxy", motion: false, ripples: false, strength: "vivid" });
	});

	it("falls back to Classic when the stored value is not a background", () => {
		localStorage.setItem(BACKGROUND_KEY, "matrix-rain");
		loadBackground();
		expect(background.id).toBe("classic");
	});

	it("reads storage that throws as the defaults", () => {
		localStorage.setItem(BACKGROUND_KEY, "aurora");
		vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
			throw new Error("SecurityError");
		});
		loadBackground();
		expect(background).toMatchObject({ id: "classic", motion: true, ripples: true, strength: "subtle" });
	});

	it("still switches this window when storage refuses the write", () => {
		vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
			throw new Error("QuotaExceededError");
		});
		expect(() => setBackground("fireflies")).not.toThrow();
		expect(background.id).toBe("fireflies");
		expect(() => setBackgroundStrength("vivid")).not.toThrow();
		expect(background.strength).toBe("vivid");
	});
});

describe("the palette's toggle", () => {
	it("turns the background off and brings back the one it had", () => {
		setBackground("aurora");
		toggleBackground();
		expect(background.id).toBe("off");
		toggleBackground();
		expect(background.id).toBe("aurora");
	});

	it("remembers that background across a restart", () => {
		setBackground("galaxy");
		setBackground("off");
		expect(localStorage.getItem(LAST_ON_KEY)).toBe("galaxy");
		background.id = "classic";
		loadBackground();
		expect(background.id).toBe("off");
		toggleBackground();
		expect(background.id).toBe("galaxy");
	});

	it("brings back Classic when nothing else was ever on", () => {
		localStorage.setItem(BACKGROUND_KEY, "off");
		loadBackground();
		toggleBackground();
		expect(background.id).toBe("classic");
	});
});

describe("following the outside world", () => {
	it("picks up a choice made in another window of the app", () => {
		stops.push(startBackground(window));
		localStorage.setItem(BACKGROUND_KEY, "constellations");
		window.dispatchEvent(new StorageEvent("storage", { key: BACKGROUND_KEY, newValue: "constellations" }));
		expect(background.id).toBe("constellations");
	});

	it("picks up a strength chosen in another window", () => {
		stops.push(startBackground(window));
		localStorage.setItem(STRENGTH_KEY, "vivid");
		window.dispatchEvent(new StorageEvent("storage", { key: STRENGTH_KEY, newValue: "vivid" }));
		expect(background.strength).toBe("vivid");
	});

	it("ignores storage events for other keys", () => {
		stops.push(startBackground(window));
		localStorage.setItem(BACKGROUND_KEY, "aurora");
		window.dispatchEvent(new StorageEvent("storage", { key: "tst-desk:theme", newValue: "dark" }));
		expect(background.id).toBe("classic");
	});

	it("follows the OS reduced-motion setting while it runs, and stops listening after", () => {
		const hook: { run: (() => void) | null } = { run: null };
		const query = {
			matches: false,
			addEventListener: (_: string, run: () => void) => (hook.run = run),
			removeEventListener: () => (hook.run = null),
		};
		const win = Object.assign(Object.create(window) as Window, {
			matchMedia: () => query as unknown as MediaQueryList,
			addEventListener: () => {},
			removeEventListener: () => {},
		});
		const stop = startBackground(win);
		expect(background.reducedMotion).toBe(false);
		query.matches = true;
		hook.run?.();
		expect(background.reducedMotion).toBe(true);
		stop();
		expect(hook.run).toBeNull();
	});

	it("is counted: a second start does not double the listeners, and the last stop removes them", () => {
		const add = vi.spyOn(window, "addEventListener");
		const remove = vi.spyOn(window, "removeEventListener");
		const first = startBackground(window);
		const second = startBackground(window);
		expect(add.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
		first();
		first();
		expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(0);
		second();
		expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
	});
});
