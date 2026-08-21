// @vitest-environment jsdom
//
// Computer-use permission panel (TD-3302): mock denied opens the same
// copy Settings reopens; Retry re-probes.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, CuPermissions, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	sendOk: true,
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
		return mocks.sendOk;
	},
}));

import {
	cuPermissions,
	startCuPermissions,
	resetCuPermissions,
	openCuPermissions,
	retryCuPermissions,
	closeCuPermissions,
} from "./cu-permissions.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function report(over: Partial<CuPermissions> = {}): CuPermissions {
	return {
		type: "cu_permissions",
		seq: 1,
		granted: false,
		screen_recording: false,
		accessibility: false,
		screen_recording_url:
			"x-apple.systemsettings:com.apple.preferences.privacy-security.ScreenCapture",
		accessibility_url:
			"x-apple.systemsettings:com.apple.preferences.privacy-security.accessibility",
		first_run: true,
		platform: "macos",
		...over,
	};
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	mocks.sendOk = true;
	resetCuPermissions();
});

describe("cu_permissions event", () => {
	it("opens the panel with both TCC names' status", () => {
		startCuPermissions();
		emit(report());
		expect(cuPermissions.open).toBe(true);
		expect(cuPermissions.granted).toBe(false);
		expect(cuPermissions.screenRecording).toBe(false);
		expect(cuPermissions.accessibility).toBe(false);
		expect(cuPermissions.screenRecordingUrl).toContain("ScreenCapture");
		expect(cuPermissions.accessibilityUrl).toContain("accessibility");
		expect(cuPermissions.firstRun).toBe(true);
	});

	it("opens on a typed permission_denied tool result", () => {
		startCuPermissions();
		emit({
			type: "tool_result",
			seq: 2,
			session_id: "sess-1",
			tool_call_id: "c1",
			status: "error",
			output: "denied",
			truncated: false,
			error_code: "permission_denied",
		});
		expect(cuPermissions.open).toBe(true);
	});
});

describe("settings reopen and retry", () => {
	it("settings reopen sends check_cu_permissions", () => {
		startCuPermissions();
		openCuPermissions();
		expect(cuPermissions.open).toBe(true);
		expect(mocks.sent).toEqual([{ type: "check_cu_permissions" }]);
		expect(cuPermissions.probing).toBe(true);
		emit(report({ first_run: false, granted: false }));
		expect(cuPermissions.probing).toBe(false);
		expect(cuPermissions.firstRun).toBe(false);
	});

	it("retry re-probes and can flip granted", () => {
		startCuPermissions();
		emit(report());
		retryCuPermissions();
		expect(mocks.sent).toEqual([{ type: "check_cu_permissions" }]);
		emit(
			report({
				granted: true,
				screen_recording: true,
				accessibility: true,
				first_run: false,
			}),
		);
		expect(cuPermissions.granted).toBe(true);
		expect(cuPermissions.probing).toBe(false);
		expect(cuPermissions.open).toBe(true);
	});

	it("close hides the panel", () => {
		startCuPermissions();
		emit(report());
		closeCuPermissions();
		expect(cuPermissions.open).toBe(false);
	});
});
