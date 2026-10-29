// Test-only mount surface for e2e/mascot.spec.ts (TD-5191, TD-5192). jsdom
// has no layout, animation clock or real pointer, so the mascot is measured
// in a real browser: the spec serves a blank page from the walk's dev
// server, loads this module and calls `window.__mascotHarness`. `at` freezes
// the entrance at one instant; `live` leaves it running so the eyes can
// follow a real cursor. Nothing in the app imports it.

import "../../app.css";
import { flushSync, mount, unmount } from "svelte";
import Mascot from "./Mascot.svelte";
import { EYE_ROW, GAZES, MASCOT_HEIGHT, bodyPath } from "../mascot";

export interface Box {
	left: number;
	right: number;
	top: number;
	width: number;
}

/** The mascot at one instant of its entrance, as drawn on screen. */
export interface MascotReading {
	/** How long the walk-in lasts, as the mascot measured it. */
	walkMs: number;
	/** The body (its eyes-ahead outline; every frame shares the outline). */
	body: Box;
	shackle: Box;
	/** How far the whole mascot leans, in degrees; negative leans left. */
	tilt: number;
	/** Which eye frame is showing. */
	eyes: string;
}

export interface MascotHarness {
	at(ms: number): MascotReading;
	/** Mount and let it run, in a stage this wide. */
	live(stageWidth: number): void;
	/** The point between the eyes, in viewport pixels. */
	eyeCentre(): { x: number; y: number };
	/** Which eyes are drawn: "dx,dy" for a held gaze, "shut", or
	 *  "idle:<frame>" while the idle loop has them. */
	eyesDrawn(): string;
}

declare global {
	interface Window {
		__mascotHarness?: MascotHarness;
	}
}

function box(el: Element | null): Box {
	if (el === null) throw new Error("mascot part missing");
	const r = el.getBoundingClientRect();
	return { left: r.left, right: r.right, top: r.top, width: r.width };
}

function tiltOf(el: Element | null): number {
	if (el === null) throw new Error("mascot part missing");
	const m = new DOMMatrixReadOnly(getComputedStyle(el).transform === "none" ? undefined : getComputedStyle(el).transform);
	return (Math.atan2(m.b, m.a) * 180) / Math.PI;
}

let current: ReturnType<typeof mount> | null = null;

function stage(): HTMLElement {
	const el = document.getElementById("stage");
	if (el === null) throw new Error("no #stage");
	return el;
}

function remount(): HTMLElement {
	const target = stage();
	if (current !== null) unmount(current);
	current = mount(Mascot, { target });
	// Mount, then the measuring effect, then its re-render.
	flushSync();
	flushSync();
	return target;
}

window.__mascotHarness = {
	at(ms) {
		const root = remount();
		// Freeze every animation on the mascot at the same instant, delays
		// included, so the reading is the frame a viewer sees at `ms`.
		for (const animation of document.getAnimations()) {
			if (animation.effect instanceof KeyframeEffect && root.contains(animation.effect.target)) {
				animation.pause();
				animation.currentTime = ms;
			}
		}
		const visible = [...root.querySelectorAll(".frame")].filter((f) => getComputedStyle(f).opacity === "1");
		return {
			walkMs: Number(root.querySelector<HTMLElement>("[data-testid='mascot']")?.dataset.walkMs ?? 0),
			body: box(root.querySelector(".frame.ahead, .held")),
			shackle: box(root.querySelector(".shackle")),
			tilt: tiltOf(root.querySelector("svg")),
			eyes: visible.map((f) => f.classList[1]).join(","),
		};
	},
	live(stageWidth) {
		stage().style.width = `${stageWidth}px`;
		remount();
	},
	eyeCentre() {
		const svg = stage().querySelector("svg");
		if (svg === null) throw new Error("mascot part missing");
		const r = svg.getBoundingClientRect();
		return { x: r.left + r.width / 2, y: r.top + (r.height * (EYE_ROW + 1)) / (MASCOT_HEIGHT + 1) };
	},
	eyesDrawn() {
		const held = stage().querySelector(".held");
		if (held !== null) {
			const d = held.getAttribute("d");
			if (d === bodyPath({ dx: 0, dy: 0, shut: true })) return "shut";
			const gaze = GAZES.find((g) => bodyPath({ ...g, shut: false }) === d);
			return gaze === undefined ? "unknown" : `${gaze.dx},${gaze.dy}`;
		}
		const shown = [...stage().querySelectorAll(".frame")].filter((f) => getComputedStyle(f).opacity === "1");
		return `idle:${shown.map((f) => f.classList[1]).join(",")}`;
	},
};
