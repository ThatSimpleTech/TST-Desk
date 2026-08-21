// @vitest-environment jsdom
//
// Screen-pane indicator store and reduced-motion contract (TD-3402).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
}));

import {
	cuIndicatorsLive,
	cursorPercent,
	pointFromArgs,
	reducedMotionIndicators,
} from "./screen-indicator";
import {
	cuIndicators,
	resetCuIndicators,
	startCuIndicators,
} from "./screen-indicator.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

describe("reduced motion", () => {
	it("is a static border with no trail", () => {
		expect(reducedMotionIndicators(true)).toEqual({ staticGlow: true, trail: false });
		expect(reducedMotionIndicators(false)).toEqual({ staticGlow: false, trail: true });
	});
});

describe("cursor placement", () => {
	it("centers when the frame size is unknown", () => {
		expect(cursorPercent(10, 20, null, null)).toEqual({ left: 50, top: 50 });
	});

	it("maps image-space points onto the frame", () => {
		expect(cursorPercent(50, 25, 100, 100)).toEqual({ left: 50, top: 25 });
	});

	it("reads x/y from tool arguments", () => {
		expect(pointFromArgs({ x: 4, y: 8 })).toEqual({ x: 4, y: 8 });
		expect(pointFromArgs({ url: "https://example.com" })).toBeNull();
	});
});

describe("liveness", () => {
	it("is live for an in-flight CU tool or an open CU turn", () => {
		expect(cuIndicatorsLive({ killed: false, inFlightCu: 1, cuTurnActive: false })).toBe(
			true,
		);
		expect(cuIndicatorsLive({ killed: false, inFlightCu: 0, cuTurnActive: true })).toBe(
			true,
		);
		expect(cuIndicatorsLive({ killed: false, inFlightCu: 0, cuTurnActive: false })).toBe(
			false,
		);
	});

	it("is not live when the kill-switch is down", () => {
		expect(cuIndicatorsLive({ killed: true, inFlightCu: 1, cuTurnActive: true })).toBe(
			false,
		);
	});
});

describe("screen-indicator store", () => {
	beforeEach(() => {
		resetCuIndicators();
		startCuIndicators();
	});

	afterEach(() => {
		resetCuIndicators();
	});

	it("goes live on a desktop tool_call and stays live after the result", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "desktop_click",
			arguments: { x: 12, y: 24 },
		});
		expect(cuIndicators.live).toBe(true);
		expect(cuIndicators.cursorX).toBe(12);
		expect(cuIndicators.cursorY).toBe(24);
		emit({
			type: "tool_result",
			seq: 2,
			session_id: "s1",
			tool_call_id: "c1",
			status: "success",
			output: "ok",
			truncated: false,
		});
		expect(cuIndicators.live).toBe(true);
	});

	it("ignores a filesystem tool", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "fs_read",
			arguments: { path: "a.md" },
		});
		expect(cuIndicators.live).toBe(false);
	});

	it("clears on turn_complete", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "browser_screenshot",
			arguments: {},
		});
		emit({
			type: "turn_complete",
			seq: 2,
			session_id: "s1",
			tokens: 1,
			cost: 0,
			tier: "worker",
			duration: 1,
			failed: false,
			error_code: null,
		});
		expect(cuIndicators.live).toBe(false);
	});

	it("clears on cancel", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "desktop_screenshot",
			arguments: {},
		});
		emit({
			type: "session_state",
			seq: 2,
			session_id: "s1",
			state: "cancelled",
		});
		expect(cuIndicators.live).toBe(false);
	});

	it("clears on cu_kill_state killed=true", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "desktop_move",
			arguments: { x: 1, y: 2 },
		});
		emit({ type: "cu_kill_state", seq: 1, killed: true });
		expect(cuIndicators.live).toBe(false);
	});

	it("records frame size from screen_frame", () => {
		emit({
			type: "screen_frame",
			seq: 3,
			session_id: "s1",
			path: "screens/aa.png",
			mime: "image/png",
			width: 800,
			height: 600,
			tool_call_id: "c2",
		});
		expect(cuIndicators.frameWidth).toBe(800);
		expect(cuIndicators.frameHeight).toBe(600);
	});
});
