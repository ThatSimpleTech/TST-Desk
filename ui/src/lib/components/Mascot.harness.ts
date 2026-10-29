// Test-only mount surface for e2e/mascot.spec.ts (TD-5191). jsdom has no
// layout or animation clock, so where the mascot and its shackle are along
// the entrance is measured in a real browser: the spec serves a blank page
// from the walk's dev server, loads this module and calls
// `window.__mascotHarness.at`. Nothing in the app imports it.

import "../../app.css";
import { flushSync, mount, unmount } from "svelte";
import Mascot from "./Mascot.svelte";

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

window.__mascotHarness = {
	at(ms) {
		const stage = document.getElementById("stage");
		if (stage === null) throw new Error("no #stage");
		if (current !== null) unmount(current);
		current = mount(Mascot, { target: stage });
		// Mount, then the measuring effect, then its re-render.
		flushSync();
		flushSync();
		// Freeze every animation on the mascot at the same instant, delays
		// included, so the reading is the frame a viewer sees at `ms`.
		for (const animation of document.getAnimations()) {
			if (animation.effect instanceof KeyframeEffect && stage.contains(animation.effect.target)) {
				animation.pause();
				animation.currentTime = ms;
			}
		}
		const visible = [...stage.querySelectorAll(".frame")].filter((f) => getComputedStyle(f).opacity === "1");
		return {
			walkMs: Number(stage.querySelector<HTMLElement>("[data-testid='mascot']")?.dataset.walkMs ?? 0),
			body: box(stage.querySelector(".frame.ahead")),
			shackle: box(stage.querySelector(".shackle")),
			tilt: tiltOf(stage.querySelector("svg")),
			eyes: visible.map((f) => f.classList[1]).join(","),
		};
	},
};
