// The shared background frame loop (TD-5202), driven by a fake host so
// frame timing is exact: nothing scheduled while stopped or resting, a
// 30 fps cap on any display, a clock that freezes on a pause, and nothing
// left behind after dispose. One loop shared by several layers steps them
// on the same frames (the window host is window-host.test.ts).

import { describe, expect, it } from "vitest";
import { createLoop, createSharedLoop, paintDue, type FrameHost } from "./loop";

class FakeHost implements FrameHost {
	private next = 1;
	private queue = new Map<number, (t: number) => void>();
	private watchers = new Set<() => void>();
	isResting = false;
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

	resting(): boolean {
		return this.isResting;
	}

	onRestChange(run: () => void): () => void {
		this.watchers.add(run);
		return () => this.watchers.delete(run);
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

	setResting(resting: boolean): void {
		this.isResting = resting;
		for (const run of this.watchers) run();
	}

	pending(): number {
		return this.queue.size;
	}

	listeners(): number {
		return this.watchers.size;
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

	it("pauses while the host rests and resumes when it wakes", () => {
		const host = new FakeHost();
		const { loop, paints } = loopOn(host);
		loop.run(true);
		let t = host.play(60, 1);
		host.setResting(true);
		expect(host.pending()).toBe(0);
		const painted = paints.length;
		t = host.play(60, 3, t);
		expect(paints.length).toBe(painted);
		host.setResting(false);
		expect(host.pending()).toBe(1);
		host.play(60, 1, t);
		expect(paints.length).toBeGreaterThan(painted + 25);
	});

	it("does not start frames when woken while stopped", () => {
		const host = new FakeHost();
		loopOn(host);
		host.setResting(true);
		host.setResting(false);
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

describe("createSharedLoop", () => {
	function shared(fps = 12) {
		const host = new FakeHost();
		return { host, tick: createSharedLoop(() => host, { fps }) };
	}

	function layer(tick: ReturnType<typeof createSharedLoop>) {
		const paints: number[] = [];
		const loop = tick.join((clock) => paints.push(clock));
		return { loop, paints };
	}

	it("steps every running layer on the same frames, at the same clock", () => {
		const { host, tick } = shared();
		const a = layer(tick);
		const b = layer(tick);
		a.loop.run(true);
		b.loop.run(true);
		host.play(60, 2);
		expect(a.paints).toEqual(b.paints);
		expect(a.paints.length).toBeGreaterThanOrEqual(23);
		expect(a.paints.length).toBeLessThanOrEqual(25);
	});

	it("keeps one frame in flight however many layers run", () => {
		const { host, tick } = shared();
		for (let i = 0; i < 3; i++) layer(tick).loop.run(true);
		expect(host.pending()).toBe(1);
		host.play(60, 1);
		expect(host.pending()).toBe(1);
		expect(host.requested).toBe(61);
	});

	it("runs while any layer runs, and paints only the running ones", () => {
		const { host, tick } = shared();
		const a = layer(tick);
		const b = layer(tick);
		a.loop.run(true);
		b.loop.run(true);
		let t = host.play(60, 1);
		a.loop.run(false);
		const aPainted = a.paints.length;
		const bPainted = b.paints.length;
		t = host.play(60, 1, t);
		expect(a.paints.length).toBe(aPainted);
		expect(b.paints.length).toBeGreaterThan(bPainted + 10);
		expect(a.loop.running()).toBe(false);
		expect(b.loop.running()).toBe(true);
	});

	it("schedules nothing once every layer stops, and carries on from the frozen clock", () => {
		const { host, tick } = shared();
		const a = layer(tick);
		const b = layer(tick);
		a.loop.run(true);
		b.loop.run(true);
		let t = host.play(60, 1);
		a.loop.run(false);
		b.loop.run(false);
		expect(host.pending()).toBe(0);
		const frozen = a.loop.clockMs();
		t = host.play(60, 5, t);
		a.loop.run(true);
		host.play(60, 0.05, t);
		expect(a.paints.at(-1)).toBe(frozen);
	});

	it("paints one layer on request, at the clock the others are at", () => {
		const { host, tick } = shared();
		const a = layer(tick);
		a.loop.run(true);
		host.play(60, 1);
		// A layer mounted later (the chat pane after the window) joins in step.
		const late = layer(tick);
		late.loop.paintNow();
		expect(late.paints).toEqual([a.loop.clockMs()]);
		expect(a.paints.at(-1)).toBe(a.loop.clockMs());
		expect(host.requested).toBe(61);
	});

	it("goes with its last layer, listeners and all, and starts a fresh clock on the next join", () => {
		const { host, tick } = shared();
		const a = layer(tick);
		const b = layer(tick);
		a.loop.run(true);
		b.loop.run(true);
		host.play(60, 1);
		a.loop.dispose();
		expect(host.pending()).toBe(1);
		expect(host.listeners()).toBe(1);
		b.loop.dispose();
		expect(host.pending()).toBe(0);
		expect(host.listeners()).toBe(0);
		// A disposed layer stays dead.
		a.loop.run(true);
		a.loop.paintNow();
		expect(host.pending()).toBe(0);
		const next = layer(tick);
		expect(next.loop.clockMs()).toBe(0);
	});
});
