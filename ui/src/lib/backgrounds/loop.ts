// The frame loop every animated background shares (TD-5202).
//
// Ambient motion needs far fewer frames than the display offers: the loop
// paints at most `fps` times a second (30 is plenty for slow drift) and
// skips the frames in between. It schedules nothing while it is stopped or
// while the document is hidden (another tab, a minimised window), and it
// keeps its own clock that only advances while frames run. A scene drawn
// from that clock freezes where it was on a pause and carries on from the
// same spot afterwards, instead of jumping ahead by the time it was away.
//
// The browser calls come in through FrameHost, so the scheduling rules are
// testable with a fake clock.

export interface FrameHost {
	request(run: (timeMs: number) => void): number;
	cancel(id: number): void;
	/** True while the document cannot be seen. */
	hidden(): boolean;
	/** Call back when visibility flips; returns the unsubscribe. */
	onVisibility(run: () => void): () => void;
}

export interface LoopOptions {
	/** Most paints a second, or null for the display's own rate. */
	fps: number | null;
	/** Paint one frame at `clockMs` on the loop's own clock. */
	paint: (clockMs: number) => void;
	/** Longest step the clock takes between two paints, so a stall never
	 *  lurches the scene forward. */
	maxStepMs?: number;
	/** Where the clock starts. */
	startMs?: number;
}

export interface Loop {
	/** Run or stop the frames. Stopped, nothing is scheduled. */
	run(on: boolean): void;
	/** Paint once at the current clock without scheduling anything (a resize,
	 *  a theme change, or the still frame). */
	paintNow(): void;
	running(): boolean;
	clockMs(): number;
	/** Stop and drop every listener. The loop is dead afterwards. */
	dispose(): void;
}

/** A 60 Hz frame lands a hair under the 33.3 ms a 30 fps paint wants;
 *  without this slack every other paint would slip a whole frame. */
const SLACK_MS = 2;

/** Whether a paint is due `sinceMs` after the last one at this frame cap. */
export function paintDue(sinceMs: number, fps: number | null): boolean {
	if (fps === null || fps <= 0) return true;
	return sinceMs >= 1000 / fps - SLACK_MS;
}

export function createLoop(host: FrameHost, options: LoopOptions): Loop {
	const maxStep = options.maxStepMs ?? 100;
	let wanted = false;
	let disposed = false;
	let frame = 0;
	let last = -1;
	let clock = options.startMs ?? 0;

	function tick(time: number): void {
		frame = 0;
		if (!wanted || disposed || host.hidden()) return;
		if (last < 0) {
			// First frame after a start or a resume: paint where the clock
			// stopped, and measure from here.
			last = time;
			options.paint(clock);
		} else if (paintDue(time - last, options.fps)) {
			clock += Math.min(Math.max(time - last, 0), maxStep);
			last = time;
			options.paint(clock);
		}
		// A paint may stop the loop (the last ripple faded); then nothing more.
		if (wanted && !disposed && frame === 0) frame = host.request(tick);
	}

	function schedule(): void {
		if (frame === 0 && wanted && !disposed && !host.hidden()) frame = host.request(tick);
	}

	function halt(): void {
		if (frame !== 0) host.cancel(frame);
		frame = 0;
		last = -1;
	}

	const offVisibility = host.onVisibility(() => {
		if (host.hidden()) halt();
		else schedule();
	});

	return {
		run(on: boolean): void {
			if (disposed) return;
			wanted = on;
			if (on) schedule();
			else halt();
		},
		paintNow(): void {
			if (!disposed) options.paint(clock);
		},
		running: () => frame !== 0,
		clockMs: () => clock,
		dispose(): void {
			if (disposed) return;
			wanted = false;
			halt();
			disposed = true;
			offVisibility();
		},
	};
}

/** The real browser behind a loop. A window without animation frames (some
 *  test environments) gets a host that never schedules, so the scene just
 *  shows its still frame. */
export function windowFrameHost(win: Window): FrameHost {
	const doc = win.document;
	const hasFrames = typeof win.requestAnimationFrame === "function";
	return {
		request: (run) => (hasFrames ? win.requestAnimationFrame(run) : 0),
		cancel: (id) => {
			if (hasFrames) win.cancelAnimationFrame(id);
		},
		hidden: () => doc.hidden === true,
		onVisibility(run) {
			doc.addEventListener("visibilitychange", run);
			return () => doc.removeEventListener("visibilitychange", run);
		},
	};
}
