// Where the mascot looks once it has landed (TD-5192): toward the cursor
// while it moves over the window, and toward the field being typed into.
// Either one holds the gaze for a moment after it stops, then hands the
// eyes back to the idle loop. Only pointer and input events the window
// already receives are used, so the cursor is followed inside this window
// and nowhere else, and nothing polls.

import { gazeToward, type Gaze } from "./mascot";

export interface GazeWatch {
	/** The point between the eyes, in viewport pixels, or null if gone. */
	eyes(): { x: number; y: number } | null;
	/** A gaze to hold, or null to hand the eyes back to the idle loop. */
	onLook(gaze: Gaze | null): void;
}

/** A cursor this close to the eyes is on the mascot: look straight out. */
const NEAR_PX = 40;
const POINTER_HOLD_MS = 2500;
const TYPING_HOLD_MS = 1500;

export function watchGaze(win: Window, watch: GazeWatch): () => void {
	let frame = 0;
	let pointer: { x: number; y: number } | null = null;
	let release: ReturnType<typeof setTimeout> | undefined;
	let held: Gaze | null = null;

	function hand(gaze: Gaze | null): void {
		if (gaze === held || (gaze !== null && held !== null && gaze.dx === held.dx && gaze.dy === held.dy)) return;
		held = gaze;
		watch.onLook(gaze);
	}

	function lookAt(point: { x: number; y: number }, holdMs: number): void {
		const eyes = watch.eyes();
		if (eyes === null) return;
		hand(gazeToward(eyes, point, NEAR_PX));
		clearTimeout(release);
		release = setTimeout(() => hand(null), holdMs);
	}

	// Pointer moves come faster than frames; look once per frame at most.
	function onPointerMove(event: PointerEvent): void {
		pointer = { x: event.clientX, y: event.clientY };
		if (frame !== 0) return;
		frame = win.requestAnimationFrame(() => {
			frame = 0;
			if (pointer !== null) lookAt(pointer, POINTER_HOLD_MS);
		});
	}

	function onInput(event: Event): void {
		const field = event.target;
		if (!(field instanceof Element)) return;
		const box = field.getBoundingClientRect();
		lookAt({ x: box.left + box.width / 2, y: box.top + box.height / 2 }, TYPING_HOLD_MS);
	}

	function onLeave(): void {
		clearTimeout(release);
		hand(null);
	}

	const root = win.document.documentElement;
	win.addEventListener("pointermove", onPointerMove, { passive: true });
	win.document.addEventListener("input", onInput, true);
	root.addEventListener("pointerleave", onLeave);
	return () => {
		win.removeEventListener("pointermove", onPointerMove);
		win.document.removeEventListener("input", onInput, true);
		root.removeEventListener("pointerleave", onLeave);
		if (frame !== 0) win.cancelAnimationFrame(frame);
		clearTimeout(release);
	};
}
