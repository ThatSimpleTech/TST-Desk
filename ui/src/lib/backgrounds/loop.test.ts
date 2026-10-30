// The shared background frame loop (TD-5202), driven by a fake host so
// frame timing is exact: nothing scheduled while stopped or hidden, a
// 30 fps cap on any display, a clock that freezes on a pause, and nothing
// left behind after dispose.

import { describe, expect, it } from "vitest";
import { createLoop, paintDue, type FrameHost } from "./loop";

class FakeHost implements FrameHost {
	private next = 1;
	private queue = new Map<number, (t: number) => void>();
	private visibility = new Set<() => void>();
	isHidden = false;
	requested = 0;
	cancelled = 0;

	request(run: (t: number) => void): number {
		this.requested++;
		const id = this.next++;
		this.queue.set(id, run);
		return id;
	}

	cancel(id: number): void {
		if (this.queue.delete(id)) this.cancelled++;
	}

	hidden(): boolean {
		return this.isHidden;
	}

	onVisibility(run: () => void): () => void {
		this.visibility.add(run);
		return () => this.visibility.delete(run);
	}

	/** Fire every queued frame at `time`. */
	frame(time: number): void {
		const runs = [...this.queue.values()];
		this.queue.clear();
		for (const run of runs) run(time);
	}

	/** Display frames at `hz` from `from` for `seconds`. */
	play(hz: number, seconds: number, from = 0): number {
		const step = 1000 / hz;
		const frames = Math.round(seconds * hz);
		for (let i = 1; i <= frames; i++) this.frame(from + i * step);
		return from + frames * step;
	}

	setHidden(hidden: boolean): void {
		this.isHidden = hidden;
		for (const run of this.visibility) run();
	}

	pending(): number {
		return this.queue.size;
	}

	listeners(): number {
		return this.visibility.size;
	}
}

function loopOn(host: FakeHost, fps: number | null = 30) {
	const paints: number[] = [];
	const loop = createLoop(host, { fps, paint: (clock) => paints.push(clock) });
	return { loop, paints };
}

describe("paintDue", () => {
	it("lets a 60 Hz frame land a hair early for 30 fps", () => {
		expect(paintDue(16.7, 30)).toBe(false);
		expect(paintDue(33.33, 30)).toBe(true);
		expect(paintDue(31.5, 30)).toBe(true);
	});

	it("paints every frame with no cap", () => {
		expect(paintDue(1, null)).toBe(true);
	});
});

describe("createLoop", () => {
	it("schedules nothing until it runs", () => {
		const host = new FakeHost();
		const { paints } = loopOn(host);
		host.play(60, 1);
		expect(host.requested).toBe(0);
		expect(paints).toEqual([]);
	});

	it("paints about 30 times a second on a 60 Hz display", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		host.play(60, 2);
		expect(paints.length).toBeGreaterThanOrEqual(58);
		expect(paints.length).toBeLessThanOrEqual(61);
	});

	it("stays near 30 fps on a 144 Hz display", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		host.play(144, 2);
		expect(paints.length).toBeGreaterThanOrEqual(54);
		expect(paints.length).toBeLessThanOrEqual(61);
	});

	it("runs at the display's rate when uncapped", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host, null);
		loop.run(true);
		host.play(60, 1);
		expect(paints.length).toBe(60);
	});

	it("advances its clock with the frames", () => {
		const host = new FakeHost();
		const { loop } = loopOn(host);
		loop.run(true);
		host.play(60, 3);
		expect(loop.clockMs()).toBeGreaterThan(2900);
		expect(loop.clockMs()).toBeLessThanOrEqual(3000);
	});

	it("stops scheduling when stopped, and freezes the clock until it runs again", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		let t = host.play(60, 1);
		loop.run(false);
		const frozen = loop.clockMs();
		const painted = paints.length;
		expect(host.pending()).toBe(0);
		t = host.play(60, 5, t);
		expect(paints.length).toBe(painted);
		loop.run(true);
		host.play(60, 0.1, t);
		// The first frame back paints where it stopped: no jump for the 5 s away.
		expect(paints[painted]).toBe(frozen);
		expect(loop.clockMs() - frozen).toBeLessThan(200);
	});

	it("pauses while the document is hidden and resumes when it is shown", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		let t = host.play(60, 1);
		host.setHidden(true);
		expect(host.pending()).toBe(0);
		const painted = paints.length;
		t = host.play(60, 3, t);
		expect(paints.length).toBe(painted);
		host.setHidden(false);
		expect(host.pending()).toBe(1);
		host.play(60, 1, t);
		expect(paints.length).toBeGreaterThan(painted + 25);
	});

	it("does not start frames when shown while stopped", () => {
		const host = new FakeHost();
		loopOn(host);
		host.setHidden(true);
		host.setHidden(false);
		expect(host.requested).toBe(0);
	});

	it("caps one step of the clock, so a stalled frame never lurches the scene", () => {
		const host = new FakeHost();
		const { loop } = loopOn(host);
		loop.run(true);
		host.frame(0);
		host.frame(5000);
		expect(loop.clockMs()).toBe(100);
	});

	it("paints once on request without scheduling anything", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.paintNow();
		expect(paints).toEqual([0]);
		expect(host.requested).toBe(0);
	});

	it("lets a paint stop the loop without one stray frame after it", () => {
		const host = new FakeHost();
		let count = 0;
		const loop = createLoop(host, {
			fps: null,
			paint: () => {
				count++;
				if (count === 3) loop.run(false);
			},
		});
		loop.run(true);
		host.play(60, 1);
		expect(count).toBe(3);
		expect(host.pending()).toBe(0);
	});

	it("leaves nothing behind after dispose", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		host.play(60, 0.5);
		loop.dispose();
		expect(host.pending()).toBe(0);
		expect(host.listeners()).toBe(0);
		const painted = paints.length;
		loop.run(true);
		loop.paintNow();
		host.play(60, 1);
		expect(paints.length).toBe(painted);
	});
});
