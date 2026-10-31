// The frame loop every animated background shares (TD-5202).
//
// Ambient motion needs far fewer frames than the display offers: the loop
// paints at most `fps` times a second (30 is plenty for slow drift) and
// skips the frames in between. It schedules nothing while it is stopped or
// while it rests: the document is hidden (another tab, a minimised window)
// or, for an ambient background, another window has the focus. It keeps its
// own clock that only advances while frames run. A scene drawn from that
// clock freezes where it was on a pause and carries on from the same spot
// afterwards, instead of jumping ahead by the time it was away.
//
// The browser calls come in through FrameHost, so the scheduling rules are
// testable with a fake clock.

export interface FrameHost {
	request(run: (timeMs: number) => void): number;
	cancel(id: number): void;
	/** True while frames should rest: the document cannot be seen, or the
	 *  host rests out of focus and the window has lost it. */
	resting(): boolean;
	/** Call back whenever resting may have flipped; returns the unsubscribe. */
	onRestChange(run: () => void): () => void;
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
		if (!wanted || disposed || host.resting()) return;
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
		if (frame === 0 && wanted && !disposed && !host.resting()) frame = host.request(tick);
	}

	function halt(): void {
		if (frame !== 0) host.cancel(frame);
		frame = 0;
		last = -1;
	}

	const offRest = host.onRestChange(() => {
		if (host.resting()) halt();
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
			offRest();
		},
	};
}

/** One loop several layers paint from. Where WebKitGTK paints in software,
 *  every frame in which any layer changes repaints the window under and
 *  over it, so layers on loops of their own cost a repaint each even at the
 *  same cap; joined here they step on the same frames, one repaint a tick. */
export interface SharedLoop {
	/** A loop for one layer: it runs, stops, paints and goes away like one
	 *  from createLoop, on the shared clock and frames. The frames run while
	 *  any joined layer runs; the last one to go takes the loop's listeners
	 *  with it, and the next join starts a fresh clock. */
	join(paint: (clockMs: number) => void): Loop;
}

export function createSharedLoop(host: () => FrameHost, options: Omit<LoopOptions, "paint">): SharedLoop {
	interface Member {
		paint: (clockMs: number) => void;
		wanted: boolean;
	}
	const members = new Set<Member>();
	let loop: Loop | null = null;

	function paintAll(clockMs: number): void {
		for (const member of members) if (member.wanted) member.paint(clockMs);
	}

	function update(): void {
		let any = false;
		for (const member of members) any ||= member.wanted;
		loop?.run(any);
	}

	return {
		join(paint) {
			loop ??= createLoop(host(), { ...options, paint: paintAll });
			const shared = loop;
			const member: Member = { paint, wanted: false };
			members.add(member);
			let gone = false;
			return {
				run(on: boolean): void {
					if (gone) return;
					member.wanted = on;
					update();
				},
				paintNow(): void {
					if (!gone) paint(shared.clockMs());
				},
				running: () => !gone && member.wanted && shared.running(),
				clockMs: () => shared.clockMs(),
				dispose(): void {
					if (gone) return;
					gone = true;
					members.delete(member);
					if (members.size > 0) {
						update();
						return;
					}
					shared.dispose();
					if (loop === shared) loop = null;
				},
			};
		},
	};
}

export interface WindowHostOptions {
	/** Rest while another window has the focus, as well as while hidden.
	 *  The ambient backgrounds do: someone working in another window is not
	 *  watching them, and where the window paints in software every frame
	 *  costs a repaint of it. The click ripples (a ring finishes in under a
	 *  second) and the Settings previews (alive under the pointer) do not. */
	restUnfocused?: boolean;
}

/** The real browser behind a loop. A window without animation frames (some
 *  test environments) gets a host that never schedules, so the scene just
 *  shows its still frame. */
export function windowFrameHost(win: Window, { restUnfocused = true }: WindowHostOptions = {}): FrameHost {
	const doc = win.document;
	const hasFrames = typeof win.requestAnimationFrame === "function";
	// Kept from the window's own focus and blur events rather than asked of
	// hasFocus() each frame: the events are what say the focus moved, so a
	// focus event always wakes the loop. Gaining focus can fire several focus
	// events in a row; each just finds the loop already scheduled.
	let focused = typeof doc.hasFocus === "function" ? doc.hasFocus() : true;
	return {
		request: (run) => (hasFrames ? win.requestAnimationFrame(run) : 0),
		cancel: (id) => {
			if (hasFrames) win.cancelAnimationFrame(id);
		},
		resting: () => doc.hidden === true || (restUnfocused && !focused),
		onRestChange(run) {
			const onFocus = (): void => {
				focused = true;
				run();
			};
			const onBlur = (): void => {
				focused = false;
				run();
			};
			doc.addEventListener("visibilitychange", run);
			if (restUnfocused) {
				win.addEventListener("focus", onFocus);
				win.addEventListener("blur", onBlur);
			}
			return () => {
				doc.removeEventListener("visibilitychange", run);
				if (restUnfocused) {
					win.removeEventListener("focus", onFocus);
					win.removeEventListener("blur", onBlur);
				}
			};
		},
	};
}
