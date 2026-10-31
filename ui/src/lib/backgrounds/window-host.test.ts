// The browser host behind the background loops (TD-5202): it rests while
// the document is hidden and, unless told otherwise, while another window
// has the focus. Out of focus nothing new is drawn and no frame is even
// scheduled; focus back, the loop carries on from the frozen clock. The
// click ripples and the Settings previews use a host that keeps going.

import { describe, expect, it, vi } from "vitest";
import { createLoop, windowFrameHost } from "./loop";

describe("windowFrameHost", () => {
	/** Just enough of a window: animation frames on a queue the test runs,
	 *  focus and blur on the window, visibility on the document. */
	function fakeWindow(focused = true) {
		const queue = new Map<number, (t: number) => void>();
		let next = 1;
		const state = { focused };
		const doc = Object.assign(new EventTarget(), { hidden: false, hasFocus: () => state.focused });
		const win = Object.assign(new EventTarget(), {
			document: doc,
			requestAnimationFrame: (run: (t: number) => void): number => {
				const id = next++;
				queue.set(id, run);
				return id;
			},
			cancelAnimationFrame: (id: number): void => {
				queue.delete(id);
			},
		});
		let now = 0;
		return {
			win,
			doc,
			queue,
			/** Display frames at 60 Hz for `seconds`. */
			play(seconds: number): void {
				for (let i = 0; i < Math.round(seconds * 60); i++) {
					now += 1000 / 60;
					const runs = [...queue.values()];
					queue.clear();
					for (const run of runs) run(now);
				}
			},
			blur(): void {
				state.focused = false;
				win.dispatchEvent(new Event("blur"));
			},
			focus(): void {
				state.focused = true;
				win.dispatchEvent(new Event("focus"));
			},
		};
	}

	function loopIn(fake: ReturnType<typeof fakeWindow>, restUnfocused?: boolean) {
		const paints: number[] = [];
		const host = windowFrameHost(fake.win as unknown as Window, restUnfocused === undefined ? {} : { restUnfocused });
		const loop = createLoop(host, { fps: 12, paint: (clock) => paints.push(clock) });
		return { loop, paints };
	}

	it("holds still while another window has the focus, and carries on from the same spot", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.play(2);
		fake.blur();
		// Nothing new is drawn: no frame is even scheduled, and the canvas or
		// styles keep the last frame.
		expect(fake.queue.size).toBe(0);
		const frozen = loop.clockMs();
		const painted = paints.length;
		fake.play(5);
		expect(paints.length).toBe(painted);
		fake.focus();
		expect(fake.queue.size).toBe(1);
		fake.play(1);
		// The first frame back paints where it stopped; the clock then moves
		// on by the second that ran, not by the five it was away.
		expect(paints[painted]).toBe(frozen);
		expect(loop.clockMs() - frozen).toBeGreaterThan(800);
		expect(loop.clockMs() - frozen).toBeLessThanOrEqual(1000);
	});

	it("starts at rest in a window without the focus, until it gets it", () => {
		const fake = fakeWindow(false);
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.play(1);
		expect(paints).toEqual([]);
		fake.focus();
		fake.play(1);
		expect(paints.length).toBeGreaterThan(10);
	});

	it("schedules one frame for a burst of focus events", () => {
		const fake = fakeWindow();
		const { loop } = loopIn(fake);
		loop.run(true);
		fake.blur();
		fake.focus();
		fake.focus();
		fake.focus();
		expect(fake.queue.size).toBe(1);
	});

	it("keeps going out of focus when told not to rest then (ripples, preview tiles)", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake, false);
		loop.run(true);
		fake.play(1);
		fake.blur();
		const painted = paints.length;
		fake.play(1);
		expect(paints.length).toBeGreaterThan(painted + 10);
	});

	it("still rests while the document is hidden, focused or not", () => {
		const fake = fakeWindow();
		const { loop } = loopIn(fake, false);
		loop.run(true);
		fake.doc.hidden = true;
		fake.doc.dispatchEvent(new Event("visibilitychange"));
		expect(fake.queue.size).toBe(0);
		fake.doc.hidden = false;
		fake.doc.dispatchEvent(new Event("visibilitychange"));
		expect(fake.queue.size).toBe(1);
	});

	it("takes its listeners with it when the loop goes", () => {
		const fake = fakeWindow();
		const added = vi.spyOn(fake.win, "addEventListener");
		const removed = vi.spyOn(fake.win, "removeEventListener");
		const { loop } = loopIn(fake);
		loop.run(true);
		loop.dispose();
		expect(added.mock.calls.map((call) => call[0]).sort()).toEqual(["blur", "focus"]);
		expect(removed.mock.calls.map((call) => call[0]).sort()).toEqual(["blur", "focus"]);
		fake.blur();
		fake.focus();
		expect(fake.queue.size).toBe(0);
	});
});
