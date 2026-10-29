// @vitest-environment jsdom
//
// Where the mascot looks once it has landed (TD-5192): toward the cursor
// while it moves, toward the field being typed into, and back to the idle
// loop when either stops or the cursor leaves the window. Driven with real
// DOM events; only the clock and the animation frame are faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Gaze } from "./mascot";
import { watchGaze } from "./mascot-gaze";

const EYES = { x: 100, y: 100 };
let looks: (Gaze | null)[];
let stop: () => void;

function move(x: number, y: number): void {
	window.dispatchEvent(new MouseEvent("pointermove", { clientX: x, clientY: y }));
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.stubGlobal("requestAnimationFrame", (run: FrameRequestCallback) => setTimeout(() => run(0), 16));
	vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
	looks = [];
	stop = watchGaze(window, { eyes: () => EYES, onLook: (gaze) => looks.push(gaze) });
});

afterEach(() => {
	stop();
	document.body.replaceChildren();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe("following the cursor", () => {
	it("looks toward the cursor, once per change of direction", () => {
		move(300, 100);
		vi.advanceTimersByTime(20);
		move(320, 110);
		vi.advanceTimersByTime(20);
		move(100, 300);
		vi.advanceTimersByTime(20);
		expect(looks).toEqual([
			{ dx: 1, dy: 0 },
			{ dx: 0, dy: 1 },
		]);
	});

	it("looks once per frame however fast the cursor moves", () => {
		move(300, 100);
		move(100, 300);
		move(-100, 100);
		vi.advanceTimersByTime(20);
		expect(looks).toEqual([{ dx: -1, dy: 0 }]);
	});

	it("hands the eyes back 2.5 seconds after the cursor stops", () => {
		move(300, 100);
		vi.advanceTimersByTime(2400);
		expect(looks).toEqual([{ dx: 1, dy: 0 }]);
		vi.advanceTimersByTime(200);
		expect(looks).toEqual([{ dx: 1, dy: 0 }, null]);
	});

	it("hands the eyes back as soon as the cursor leaves the window", () => {
		move(300, 100);
		vi.advanceTimersByTime(20);
		document.documentElement.dispatchEvent(new MouseEvent("pointerleave"));
		expect(looks).toEqual([{ dx: 1, dy: 0 }, null]);
		vi.advanceTimersByTime(3000);
		expect(looks).toHaveLength(2);
	});
});

describe("glancing at typing", () => {
	it("looks at the field being typed into, then back after 1.5 seconds", () => {
		const field = document.createElement("textarea");
		field.getBoundingClientRect = () => new DOMRect(60, 400, 80, 40);
		document.body.appendChild(field);

		field.dispatchEvent(new Event("input", { bubbles: true }));
		expect(looks).toEqual([{ dx: 0, dy: 1 }]);
		vi.advanceTimersByTime(1400);
		field.dispatchEvent(new Event("input", { bubbles: true }));
		vi.advanceTimersByTime(1400);
		expect(looks).toEqual([{ dx: 0, dy: 1 }]);
		vi.advanceTimersByTime(200);
		expect(looks).toEqual([{ dx: 0, dy: 1 }, null]);
	});
});

it("stops listening when stopped", () => {
	stop();
	move(300, 100);
	vi.advanceTimersByTime(3000);
	expect(looks).toEqual([]);
});
