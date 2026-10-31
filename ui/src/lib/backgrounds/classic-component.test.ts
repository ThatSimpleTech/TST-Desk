// @vitest-environment jsdom
//
// Classic as the window mounts it (TD-5202): the orbit, the header bands and
// the dust step on one loop, on the same frames; Motion off shows the old
// resting pose; picking another background leaves no frame or listener
// behind. Every background holds its frame while another window has the
// focus and carries on from it after; a ripple already lit still finishes.
// Canvas calls go to a recording fake context — jsdom has no 2D canvas —
// and animation frames to a queue the test drives, in a window that has the
// focus until a test takes it away.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import BackgroundHost from "../components/backgrounds/BackgroundHost.svelte";
import { CLASSIC_FPS } from "./classic";
import { background, loadBackground, setBackground, setBackgroundMotion } from "./store.svelte.js";

let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let requested: number;
let paints: string[];
let mounted: ReturnType<typeof mount>[];
let target: HTMLElement;
let now: number;
let hasFocus: ReturnType<typeof vi.spyOn>;

/** A 2D context that records what is drawn and does nothing. */
function fakeContext(): CanvasRenderingContext2D {
	const gradient = { addColorStop: () => {} };
	const state: Record<string | symbol, unknown> = {};
	return new Proxy(state, {
		get(store, prop) {
			if (prop in store) return store[prop];
			if (prop === "createRadialGradient" || prop === "createLinearGradient") return () => gradient;
			if (prop === "createImageData")
				return (w: number, h: number) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) });
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

/** Run the queued animation frames `count` times, 1/60 s apart, on a clock
 *  that only moves forward, then let Svelte write what they changed. */
function runFrames(count: number): void {
	for (let i = 0; i < count; i++) {
		now += 1000 / 60;
		const queued = [...frames.values()];
		frames.clear();
		for (const run of queued) run(now);
	}
	flushSync();
}

/** Another window takes the focus, or gives it back. */
function blurWindow(): void {
	hasFocus.mockReturnValue(false);
	window.dispatchEvent(new Event("blur"));
	flushSync();
}

function focusWindow(): void {
	hasFocus.mockReturnValue(true);
	window.dispatchEvent(new Event("focus"));
	flushSync();
}

function orbitField(): HTMLElement {
	const field = target.querySelector<HTMLElement>(".orbit .field");
	if (field === null) throw new Error("no orbit");
	return field;
}

function bandDrifts(): string[] {
	return [...target.querySelectorAll<HTMLElement>(".band")].map((band) => band.style.getPropertyValue("--drift"));
}

/** Where the orbit's clock stands, read back from its turn (one turn in 80 s). */
function orbitClock(): number {
	const turn = /rotate\(([-\d.e]+)deg\)/.exec(orbitField().style.transform);
	if (turn === null) throw new Error(`orbit not turning: "${orbitField().style.transform}"`);
	return (Number(turn[1]) / 360) * 80_000;
}

beforeEach(() => {
	frames = new Map();
	nextFrame = 1;
	requested = 0;
	paints = [];
	mounted = [];
	now = 1000;
	hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(true);
	vi.spyOn(window, "requestAnimationFrame").mockImplementation((run) => {
		requested++;
		const id = nextFrame++;
		frames.set(id, run);
		return id;
	});
	vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
		frames.delete(id);
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

describe("Classic", () => {
	it("steps the orbit, the header bands and the dust on one loop, on the same frames", () => {
		setBackground("classic");
		render("window");
		render("chat");
		// Three moving layers, one frame in flight (the ripples are idle).
		expect(frames.size).toBe(1);
		runFrames(1);
		let ticks = 0;
		let field = orbitField().style.transform;
		let drifts = bandDrifts();
		for (let i = 0; i < 60; i++) {
			paints = [];
			runFrames(1);
			expect(frames.size).toBe(1);
			const turned = orbitField().style.transform !== field;
			const drifted = bandDrifts().map((d, band) => d !== drifts[band]);
			const dusted = paints.includes("clearRect");
			expect([drifted[0], drifted[1], dusted], `frame ${i}`).toEqual([turned, turned, turned]);
			if (turned) ticks++;
			field = orbitField().style.transform;
			drifts = bandDrifts();
		}
		expect(ticks).toBeGreaterThanOrEqual(CLASSIC_FPS - 1);
		expect(ticks).toBeLessThanOrEqual(CLASSIC_FPS + 1);
	});

	it("shows the stylesheet's resting pose with Motion off, and schedules nothing", () => {
		setBackground("classic");
		setBackgroundMotion(false);
		render("window");
		render("chat");
		runFrames(5);
		expect(requested).toBe(0);
		expect(orbitField().style.transform).toBe("");
		expect(target.querySelector<HTMLElement>(".orbit .wash")?.style.opacity).toBe("");
		expect(bandDrifts()).toEqual(["", ""]);
	});

	it("drops back to the resting pose when Motion goes off while it runs, as the CSS did", () => {
		setBackground("classic");
		render("window");
		render("chat");
		runFrames(20);
		expect(orbitField().style.transform).not.toBe("");
		setBackgroundMotion(false);
		flushSync();
		expect(frames.size).toBe(0);
		expect(orbitField().style.transform).toBe("");
		expect(bandDrifts()).toEqual(["", ""]);
	});

	it("leaves no frame and no listener behind when another background is picked", () => {
		const added = vi.spyOn(window, "addEventListener");
		const removed = vi.spyOn(window, "removeEventListener");
		setBackground("classic");
		render("window");
		render("chat");
		runFrames(10);
		setBackground("off");
		flushSync();
		expect(frames.size).toBe(0);
		const focusListeners = (spy: typeof added) => spy.mock.calls.filter(([type]) => type === "focus" || type === "blur").length;
		expect(focusListeners(added)).toBeGreaterThan(0);
		expect(focusListeners(removed)).toBe(focusListeners(added));
	});

});

describe("out of focus", () => {
	it("Classic holds its frame and schedules nothing, then carries on from the same spot", () => {
		setBackground("classic");
		render("window");
		render("chat");
		runFrames(31);
		const held = orbitClock();
		const drifts = bandDrifts();
		blurWindow();
		expect(frames.size).toBe(0);
		const asked = requested;
		paints = [];
		runFrames(120);
		expect(requested).toBe(asked);
		expect(paints).toEqual([]);
		expect(orbitClock()).toBe(held);
		expect(bandDrifts()).toEqual(drifts);
		focusWindow();
		expect(frames.size).toBe(1);
		// The first frame back paints the frozen clock, the next tick moves
		// one tick on: no jump for the two seconds away.
		runFrames(1);
		expect(orbitClock()).toBe(held);
		runFrames(5);
		expect(orbitClock() - held).toBeGreaterThan(50);
		expect(orbitClock() - held).toBeLessThanOrEqual(100);
	});

	it("a drawn background schedules nothing until focus comes back", () => {
		setBackground("deep-space");
		render("window");
		runFrames(3);
		blurWindow();
		expect(frames.size).toBe(0);
		const before = requested;
		paints = [];
		runFrames(10);
		expect(requested).toBe(before);
		expect(paints).toEqual([]);
		focusWindow();
		expect(frames.size).toBe(1);
		runFrames(4);
		expect(paints).toContain("clearRect");
	});

	it("a ripple already lit still finishes", () => {
		setBackground("galaxy");
		setBackgroundMotion(false);
		render("chat");
		window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 0, clientY: 0 }));
		blurWindow();
		expect(frames.size).toBe(1);
	});
});
