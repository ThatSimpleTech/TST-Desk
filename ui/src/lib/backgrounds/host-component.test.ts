// @vitest-environment jsdom
//
// The background host (TD-5202) as the window mounts it: Off draws nothing
// and schedules no animation frame, even when clicked; a moving background
// runs frames; Motion off and the OS reduced-motion setting give one still
// paint and no frames; switching tears the old loop down; ripples come and
// go with their switch; the veil is one sheet over the window layer and
// never in the chat pane, and Vivid thins it to a share of the same token; a window layer's backing store is capped at 1.5×
// and follows a move to a screen of another density (Classic's one loop and
// the rest out of focus are classic-component.test.ts). Canvas calls go to a
// recording fake context — jsdom has no 2D canvas — and animation frames to
// a queue the test drives, in a focused window.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { flushSync, mount, unmount } from "svelte";
import BackgroundHost from "../components/backgrounds/BackgroundHost.svelte";
import { WINDOW_LAYERS, WINDOW_VEILS, veilFill } from "../components/backgrounds/registry";
import { BACKGROUND_IDS } from "./catalog";
import {
	background,
	loadBackground,
	setBackground,
	setBackgroundMotion,
	setBackgroundRipples,
	setBackgroundStrength,
} from "./store.svelte.js";

let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let requested: number;
let cancelled: number;
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

beforeEach(() => {
	frames = new Map();
	nextFrame = 1;
	now = 1000;
	hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(true);
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
	vi.unstubAllGlobals();
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

describe("the backing store", () => {
	it("is capped at 1.5× behind the window, and follows a move to a screen of another density", () => {
		let ratio = 2;
		const listeners = new Map<string, Set<() => void>>();
		vi.spyOn(window, "devicePixelRatio", "get").mockImplementation(() => ratio);
		vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(400);
		vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
		vi.stubGlobal("matchMedia", (media: string) => {
			const set = listeners.get(media) ?? new Set<() => void>();
			listeners.set(media, set);
			return {
				media,
				matches: false,
				addEventListener: (_type: string, run: () => void) => set.add(run),
				removeEventListener: (_type: string, run: () => void) => set.delete(run),
			};
		});
		setBackground("deep-space");
		render("window");
		const canvas = target.querySelector("canvas");
		expect([canvas?.width, canvas?.height]).toEqual([600, 450]);
		// Same size, new density: only the resolution query notices.
		ratio = 1;
		for (const run of [...(listeners.get("(resolution: 2dppx)") ?? [])]) run();
		expect([canvas?.width, canvas?.height]).toEqual([400, 300]);
		expect(listeners.get("(resolution: 1dppx)")?.size).toBe(1);
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

});

describe("the veil", () => {
	it("is one even sheet laid over the window layer, for every background but Classic and Off", () => {
		render("window");
		for (const id of BACKGROUND_IDS) {
			setBackground(id);
			flushSync();
			const veils = [...target.querySelectorAll<HTMLElement>(".veil")];
			const token = WINDOW_VEILS[id];
			expect(veils.length, id).toBe(token === null ? 0 : 1);
			if (token === null) continue;
			expect(veils[0].style.background, id).toBe(`var(${token})`);
			// Painted after the background's own layers, so it lies on top.
			const layers = [...target.children].filter((el) => !el.classList.contains("veil"));
			expect(layers.length, id).toBeGreaterThan(0);
			for (const el of layers) {
				expect(el.compareDocumentPosition(veils[0]) & Node.DOCUMENT_POSITION_FOLLOWING, id).toBeTruthy();
			}
		}
	});

	it("thins to Vivid's share of the same token, only where there is a veil to thin", () => {
		render("window");
		setBackgroundStrength("vivid");
		for (const id of BACKGROUND_IDS) {
			setBackground(id);
			flushSync();
			const veils = [...target.querySelectorAll<HTMLElement>(".veil")];
			const token = WINDOW_VEILS[id];
			// Off and Classic have none, at any strength.
			expect(veils.length, id).toBe(token === null ? 0 : 1);
			if (token === null) continue;
			// The same token at Vivid's share: its own where it sets one,
			// else the default.
			expect(veils[0].style.background, id).toBe(
				`color-mix(in srgb, var(${token}) var(${token}-vivid, var(--bg-veil-vivid)), transparent)`,
			);
			// A fill, not an opacity: no transparency layer over the window.
			expect(veils[0].style.opacity, id).toBe("");
		}
		setBackgroundStrength("subtle");
		flushSync();
		const token = WINDOW_VEILS[background.id];
		expect(target.querySelector<HTMLElement>(".veil")?.style.background).toBe(`var(${token})`);
	});

	it("keeps Subtle as the token itself", () => {
		expect(veilFill("--bg-galaxy-veil", "subtle")).toBe("var(--bg-galaxy-veil)");
	});

	it("never sits in the chat pane, so nothing steps where the chat meets the Activity pane", () => {
		render("chat");
		for (const id of BACKGROUND_IDS) {
			setBackground(id);
			flushSync();
			expect(target.querySelector(".veil"), id).toBeNull();
		}
	});

	it("reads a token every theme declares", () => {
		const css = readFileSync(resolve(process.cwd(), "src/lib/tokens.css"), "utf-8");
		for (const token of Object.values(WINDOW_VEILS)) {
			if (token === null) continue;
			// The light root, the dark media query and the forced dark theme.
			expect(css.split(`${token}:`).length - 1, token).toBe(3);
		}
	});
});
