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
	 *  from createLoop, on the shared clock and frames. The frames, and the
	 *  host's listeners, live while any joined layer runs. When the last one
	 *  stops, the clock goes back to its start (see update below). */
	join(paint: (clockMs: number) => void): Loop;
}

export function createSharedLoop(host: () => FrameHost, options: Omit<LoopOptions, "paint">): SharedLoop {
	interface Member {
		paint: (clockMs: number) => void;
		wanted: boolean;
	}
	const members = new Set<Member>();
	const startMs = options.startMs ?? 0;
	/** The frames, made when a layer starts running and dropped when none is. */
	let loop: Loop | null = null;

	function paintAll(clockMs: number): void {
		for (const member of members) if (member.wanted) member.paint(clockMs);
	}

	const clock = (): number => loop?.clockMs() ?? startMs;

	function update(): void {
		let any = false;
		for (const member of members) any ||= member.wanted;
		if (any) {
			loop ??= createLoop(host(), { ...options, paint: paintAll });
			loop.run(true);
			return;
		}
		if (loop === null) return;
		// Every layer stopped: Motion off, reduced motion, or the background
		// going. A rest (hidden, or another window focused) is not a stop: the
		// loop sits that out itself and keeps its clock. Classic's orbit and
		// bands drop to their resting pose on a stop, the old CSS's first
		// frame, so the clock goes back to its start and the layers still
		// joined (the dust, which keeps its canvas) repaint there. Motion on
		// again then starts from the resting pose instead of jumping to where
		// the clock stood.
		loop.dispose();
		loop = null;
		for (const member of members) member.paint(startMs);
	}

	return {
		join(paint) {
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
					if (!gone) paint(clock());
				},
				running: () => !gone && member.wanted && (loop?.running() ?? false),
				clockMs: clock,
				dispose(): void {
					if (gone) return;
					gone = true;
					members.delete(member);
					update();
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

/** How often to ask whether the page still has the focus while that focus
 *  is inside one of its iframes, where the window's own events are silent. */
export const FRAME_FOCUS_POLL_MS = 500;

/** The real browser behind a loop. A window without animation frames (some
 *  test environments) gets a host that never schedules, so the scene just
 *  shows its still frame. */
export function windowFrameHost(win: Window, { restUnfocused = true }: WindowHostOptions = {}): FrameHost {
	const doc = win.document;
	const hasFrames = typeof win.requestAnimationFrame === "function";
	const pageFocused = (): boolean => (typeof doc.hasFocus === "function" ? doc.hasFocus() : true);
	// A click into an iframe of this page (an artifact's sandboxed preview,
	// the Preview pane) blurs the window although the page keeps the focus,
	// and from then on the window hears nothing: not when the app is left
	// from inside the frame, nor when the focus comes back into it. While the
	// focus is in a frame, the page's own hasFocus() is the answer instead.
	const focusInFrame = (): boolean => doc.activeElement?.localName === "iframe";
	// Otherwise kept from the window's own focus and blur events rather than
	// asked of hasFocus() each frame: the events are what say the focus
	// moved, so a focus event always wakes the loop. Gaining focus can fire
	// several focus events in a row; each just finds the loop already
	// scheduled.
	let focused = pageFocused();
	const unfocused = (): boolean => !(focusInFrame() ? pageFocused() : focused);
	return {
		request: (run) => (hasFrames ? win.requestAnimationFrame(run) : 0),
		cancel: (id) => {
			if (hasFrames) win.cancelAnimationFrame(id);
		},
		resting: () => doc.hidden === true || (restUnfocused && unfocused()),
		onRestChange(run) {
			let recheck = 0;
			let poll = 0;
			const stopPolling = (): void => {
				if (poll !== 0) win.clearInterval(poll);
				poll = 0;
			};
			// While the focus is in a frame, look twice a second: the loop
			// rests or wakes by what resting() says now. Every look calls
			// run(), which does nothing when nothing changed; comparing with
			// the last look instead would miss a trip out of the app and back
			// between two looks, after a frame had already seen it rest. Once
			// the focus is back out (or the frame went away with it) the
			// window's events take over again.
			const watchFrame = (): void => {
				if (poll !== 0 || !focusInFrame()) return;
				poll = win.setInterval(() => {
					if (!focusInFrame()) {
						stopPolling();
						focused = pageFocused();
					}
					run();
				}, FRAME_FOCUS_POLL_MS);
			};
			const onFocus = (): void => {
				focused = true;
				if (!focusInFrame()) stopPolling();
				run();
			};
			const onBlur = (): void => {
				focused = false;
				watchFrame();
				run();
				// An engine may move the focus into the frame only after this
				// event; look again once it has landed.
				if (recheck !== 0) win.clearTimeout(recheck);
				recheck = win.setTimeout(() => {
					recheck = 0;
					watchFrame();
					run();
				}, 0);
			};
			doc.addEventListener("visibilitychange", run);
			if (restUnfocused) {
				win.addEventListener("focus", onFocus);
				win.addEventListener("blur", onBlur);
				// Made while the focus already sits in a frame (a background
				// switched from another window): watch it from the start.
				watchFrame();
			}
			return () => {
				doc.removeEventListener("visibilitychange", run);
				if (restUnfocused) {
					win.removeEventListener("focus", onFocus);
					win.removeEventListener("blur", onBlur);
				}
				if (recheck !== 0) win.clearTimeout(recheck);
				recheck = 0;
				stopPolling();
			};
		},
	};
}
