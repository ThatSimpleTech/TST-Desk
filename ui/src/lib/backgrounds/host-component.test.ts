// @vitest-environment jsdom
//
// The background host (TD-5202) as the window mounts it: Off draws nothing
// and schedules no animation frame, even when clicked; a moving background
// runs frames; Motion off and the OS reduced-motion setting give one still
// paint and no frames; switching tears the old loop down; ripples come and
// go with their switch. Canvas calls go to a recording fake context — jsdom
// has no 2D canvas — and animation frames to a queue the test drives.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import BackgroundHost from "../components/backgrounds/BackgroundHost.svelte";
import { WINDOW_LAYERS } from "../components/backgrounds/registry";
import { BACKGROUND_IDS } from "./catalog";
import { background, loadBackground, setBackground, setBackgroundMotion, setBackgroundRipples } from "./store.svelte.js";

let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let requested: number;
let cancelled: number;
let paints: string[];
let mounted: ReturnType<typeof mount>[];
let target: HTMLElement;

/** A 2D context that records what is drawn and does nothing. */
function fakeContext(): CanvasRenderingContext2D {
	const gradient = { addColorStop: () => {} };
	const state: Record<string | symbol, unknown> = {};
	return new Proxy(state, {
		get(store, prop) {
			if (prop in store) return store[prop];
			if (prop === "createRadialGradient" || prop === "createLinearGradient") return () => gradient;
			return () => paints.push(String(prop));
		},
		set(store, prop, value) {
			store[prop] = value;
			return true;
		},
	}) as unknown as CanvasRenderingContext2D;
}

function render(layer: "window" | "chat"): void {
	mounted.push(mount(BackgroundHost, { target, props: { layer } }));
	flushSync();
}

/** Run the queued animation frames `count` times, 1/60 s apart. */
function runFrames(count: number): void {
	for (let i = 0; i < count; i++) {
		const queued = [...frames.values()];
		frames.clear();
		for (const run of queued) run(performance.now() + i * 16.7);
	}
}

beforeEach(() => {
	frames = new Map();
	nextFrame = 1;
	requested = 0;
	cancelled = 0;
	paints = [];
	mounted = [];
	vi.spyOn(window, "requestAnimationFrame").mockImplementation((run) => {
		requested++;
		const id = nextFrame++;
		frames.set(id, run);
		return id;
	});
	vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
		if (frames.delete(id)) cancelled++;
	});
	vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
		() => fakeContext() as unknown as ReturnType<HTMLCanvasElement["getContext"]>,
	);
	localStorage.clear();
	loadBackground();
	background.reducedMotion = false;
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	for (const component of mounted) unmount(component);
	target.remove();
	vi.restoreAllMocks();
	localStorage.clear();
});

describe("Off", () => {
	it("renders nothing, schedules no animation frame, and ignores clicks", () => {
		setBackground("off");
		render("window");
		render("chat");
		expect(target.querySelector("canvas")).toBeNull();
		expect(target.querySelector("[aria-hidden='true']")).toBeNull();
		window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10 }));
		runFrames(5);
		expect(requested).toBe(0);
		expect(paints).toEqual([]);
	});

	it("stops everything the moment it is picked", () => {
		setBackground("galaxy");
		render("window");
		render("chat");
		expect(requested).toBeGreaterThan(0);
		setBackground("off");
		flushSync();
		expect(frames.size).toBe(0);
		const before = requested;
		runFrames(10);
		window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10 }));
		expect(requested).toBe(before);
		expect(target.querySelector("canvas")).toBeNull();
	});
});

describe("a moving background", () => {
	it("runs frames and paints into its canvas", () => {
		setBackground("deep-space");
		render("window");
		expect(target.querySelector("canvas")).not.toBeNull();
		runFrames(4);
		expect(requested).toBeGreaterThan(1);
		expect(paints).toContain("clearRect");
	});

	it("swaps in place: the old loop is cancelled, the new one runs", () => {
		setBackground("galaxy");
		render("window");
		setBackground("constellations");
		flushSync();
		expect(cancelled).toBeGreaterThan(0);
		expect(frames.size).toBe(1);
	});

	it("gives every background but Off a window layer", () => {
		for (const id of BACKGROUND_IDS) expect(WINDOW_LAYERS[id] === null).toBe(id === "off");
	});
});

describe("holding still", () => {
	it("Motion off paints one still frame and schedules nothing", () => {
		setBackground("aurora");
		setBackgroundMotion(false);
		render("window");
		expect(paints).toContain("clearRect");
		expect(requested).toBe(0);
	});

	it("turning Motion back on starts the frames again", () => {
		setBackground("fireflies");
		setBackgroundMotion(false);
		render("window");
		setBackgroundMotion(true);
		flushSync();
		expect(requested).toBe(1);
	});

	it("the OS reduced-motion setting holds still and turns ripples off", () => {
		background.reducedMotion = true;
		setBackground("classic");
		render("window");
		render("chat");
		expect(requested).toBe(0);
		expect(target.querySelector(".ripples")).toBeNull();
		window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10 }));
		expect(requested).toBe(0);
	});
});

describe("click ripples", () => {
	it("are drawn by their own layer, idle until a click, then run frames", () => {
		setBackground("galaxy");
		setBackgroundMotion(false);
		render("chat");
		expect(target.querySelector(".ripples canvas")).not.toBeNull();
		expect(requested).toBe(0);
		window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 0, clientY: 0 }));
		expect(requested).toBe(1);
	});

	it("go away with their switch", () => {
		setBackground("galaxy");
		render("chat");
		setBackgroundRipples(false);
		flushSync();
		expect(target.querySelector(".ripples")).toBeNull();
	});

	it("sit over a veil on every background but Classic and Off", () => {
		setBackground("deep-space");
		render("chat");
		expect(target.querySelector(".veil")).not.toBeNull();
		setBackground("classic");
		flushSync();
		expect(target.querySelector(".veil")).toBeNull();
	});
});
