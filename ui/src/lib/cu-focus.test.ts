// TD-4832: the focus_window event activates the host window through the
// bridge; outside Tauri (or in tests without a bridge) it is a no-op, and
// a failed activation never surfaces.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	tauri: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onDaemonEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
}));

vi.mock("./open-file", () => ({
	isTauri: () => mocks.tauri,
}));

import { startCuFocus, resetCuFocus, type CuFocusBridge } from "./cu-focus.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function focusEvent(): DaemonEventUnion {
	return { type: "focus_window", seq: 1, reason: "cu_session_closed" } as DaemonEventUnion;
}

describe("cu-focus", () => {
	beforeEach(() => {
		mocks.handler = null;
		mocks.tauri = true;
	});

	afterEach(() => {
		resetCuFocus();
	});

	it("activates the window on focus_window", async () => {
		const activate = vi.fn().mockResolvedValue(undefined);
		const bridge: CuFocusBridge = { activate };
		const off = startCuFocus(bridge);
		emit(focusEvent());
		await vi.waitFor(() => expect(activate).toHaveBeenCalledTimes(1));
		off();
	});

	it("ignores unrelated events", async () => {
		const activate = vi.fn().mockResolvedValue(undefined);
		const off = startCuFocus({ activate });
		emit({ type: "cu_session", session_id: "sess-1", active: true, seq: 2 });
		await new Promise((r) => setTimeout(r, 10));
		expect(activate).not.toHaveBeenCalled();
		off();
	});

	it("is a no-op outside Tauri", async () => {
		mocks.tauri = false;
		const off = startCuFocus();
		emit(focusEvent());
		// No bridge, no throw — the event is simply dropped.
		off();
	});

	it("swallows an activation failure", async () => {
		const activate = vi.fn().mockRejectedValue(new Error("no window"));
		const off = startCuFocus({ activate });
		emit(focusEvent());
		await vi.waitFor(() => expect(activate).toHaveBeenCalledTimes(1));
		// Resolution, not rejection, reaches here.
		off();
	});
});
