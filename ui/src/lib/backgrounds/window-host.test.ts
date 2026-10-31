// The browser host behind the background loops (TD-5202): it rests while
// the document is hidden and, unless told otherwise, while another window
// has the focus. Out of focus nothing new is drawn and no frame is even
// scheduled; focus back, the loop carries on from the frozen clock. The
// click ripples and the Settings previews use a host that keeps going.
// Focus inside one of the page's iframes (an artifact or a preview) blurs
// the window but not the page: the loop keeps going, and rests when the app
// is left from inside the frame.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FRAME_FOCUS_POLL_MS, createLoop, windowFrameHost } from "./loop";

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
});

describe("windowFrameHost", () => {
	/** Just enough of a window: animation frames on a queue the test runs,
	 *  focus and blur on the window, visibility on the document. */
	function fakeWindow(focused = true) {
		const queue = new Map<number, (t: number) => void>();
		let next = 1;
		const state = { focused };
		const doc = Object.assign(new EventTarget(), {
			hidden: false,
			hasFocus: () => state.focused,
			activeElement: { localName: "body" } as { localName: string } | null,
		});
		const win = Object.assign(new EventTarget(), {
			document: doc,
			// The test's fake timers, looked up on each call.
			setTimeout: (run: () => void, ms?: number) => setTimeout(run, ms),
			clearTimeout: (id: number) => clearTimeout(id),
			setInterval: (run: () => void, ms?: number) => setInterval(run, ms),
			clearInterval: (id: number) => clearInterval(id),
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
				doc.activeElement = { localName: "body" };
				win.dispatchEvent(new Event("focus"));
			},
			/** A click into an iframe of the page: the window blurs, the page
			 *  keeps the focus. `late` moves the focus there only after the
			 *  blur event, as an engine may. */
			intoFrame(late = false): void {
				const land = (): void => {
					doc.activeElement = { localName: "iframe" };
					state.focused = true;
				};
				if (late) {
					state.focused = false;
					win.dispatchEvent(new Event("blur"));
					land();
				} else {
					land();
					win.dispatchEvent(new Event("blur"));
				}
			},
			/** Another app takes the focus, or gives it back, while it sits
			 *  in the frame: the window hears nothing. */
			setPageFocus(on: boolean): void {
				state.focused = on;
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

	it("keeps going when the focus moves into one of the page's iframes, which blurs only the window", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.play(1);
		fake.intoFrame();
		expect(fake.queue.size).toBe(1);
		const painted = paints.length;
		fake.play(1);
		expect(paints.length).toBeGreaterThan(painted + 10);
	});

	it("looks again once the focus has landed, when the engine moves it into the frame after the blur", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.play(1);
		fake.intoFrame(true);
		vi.advanceTimersByTime(0);
		expect(fake.queue.size).toBe(1);
		const painted = paints.length;
		fake.play(1);
		expect(paints.length).toBeGreaterThan(painted + 10);
	});

	it("rests when the app is left from inside the frame, and wakes when the focus comes back into it", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.intoFrame();
		fake.play(1);
		// No event reaches the window: the next frame asks the page itself,
		// and nothing more is scheduled.
		fake.setPageFocus(false);
		fake.play(0.1);
		expect(fake.queue.size).toBe(0);
		const frozen = loop.clockMs();
		const painted = paints.length;
		vi.advanceTimersByTime(FRAME_FOCUS_POLL_MS * 4);
		fake.play(2);
		expect(paints.length).toBe(painted);
		// Back into the frame, still without a window event: the watch wakes
		// the loop, which carries on from where it stopped.
		fake.setPageFocus(true);
		vi.advanceTimersByTime(FRAME_FOCUS_POLL_MS);
		expect(fake.queue.size).toBe(1);
		fake.play(1);
		expect(paints[painted]).toBe(frozen);
		expect(paints.length).toBeGreaterThan(painted + 10);
	});

	it("wakes after a trip out of the app and back that falls between two looks", () => {
		const fake = fakeWindow();
		const { loop, paints } = loopIn(fake);
		loop.run(true);
		fake.intoFrame();
		fake.play(1);
		fake.setPageFocus(false);
		fake.play(0.1);
		expect(fake.queue.size).toBe(0);
		fake.setPageFocus(true);
		vi.advanceTimersByTime(FRAME_FOCUS_POLL_MS);
		expect(fake.queue.size).toBe(1);
		const painted = paints.length;
		fake.play(1);
		expect(paints.length).toBeGreaterThan(painted + 10);
	});

	it("stops asking once the focus is back in the page, and leaves no timer when the loop goes", () => {
		const fake = fakeWindow();
		const { loop } = loopIn(fake);
		loop.run(true);
		fake.intoFrame();
		vi.advanceTimersByTime(0);
		expect(vi.getTimerCount()).toBe(1);
		fake.focus();
		expect(vi.getTimerCount()).toBe(0);
		fake.intoFrame();
		loop.dispose();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("still rests on a real switch to another app, after looking again", () => {
		const fake = fakeWindow();
		const { loop } = loopIn(fake);
		loop.run(true);
		fake.blur();
		vi.advanceTimersByTime(FRAME_FOCUS_POLL_MS * 2);
		expect(fake.queue.size).toBe(0);
		expect(vi.getTimerCount()).toBe(0);
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
