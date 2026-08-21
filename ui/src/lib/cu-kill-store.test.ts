// Kill-switch store (TD-3404).
//
// The chrome must not invent the latch: it only flips on `cu_kill_state`,
// and the palette / title-bar / shortcut all send the same `set_cu_kill`.

import { describe, it, expect, beforeEach } from "vitest";
import { vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import { cuKill, startCuKill, setCuKill, resetCuKill } from "./cu-kill.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	resetCuKill();
	mocks.sent.length = 0;
	mocks.handler = null;
});

describe("cu-kill store", () => {
	it("starts not-killed and ignores every other event", () => {
		startCuKill();
		expect(cuKill.killed).toBe(false);
		emit({ type: "error", seq: 1, code: "x", message: "no" });
		expect(cuKill.killed).toBe(false);
	});

	it("sends set_cu_kill and flips only when the daemon acks", () => {
		startCuKill();
		setCuKill(true);
		expect(mocks.sent).toEqual([{ type: "set_cu_kill", killed: true }]);
		expect(cuKill.killed).toBe(false);
		emit({ type: "cu_kill_state", seq: 1, killed: true });
		expect(cuKill.killed).toBe(true);
		setCuKill(false);
		emit({ type: "cu_kill_state", seq: 1, killed: false });
		expect(cuKill.killed).toBe(false);
	});
});
