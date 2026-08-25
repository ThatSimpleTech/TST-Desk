// @vitest-environment jsdom
//
// Computer-use permission panel (TD-3302, TD-3303): mock denied opens
// the same copy Settings reopens; Windows first-run is integrity copy;
// Retry re-probes.

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
		no_gate: "",
		uipi: "",
		secure_desktop: "",
		elevated: false,
		uipi_applies: false,
		secure_desktop_applies: false,
		session_type: "",
		wayland: "",
		wayland_applies: false,
		xtest: "",
		xtest_applies: false,
		no_display: "",
		no_display_applies: false,
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

	it("opens Windows copy on a first-run windows event", () => {
		startCuPermissions();
		emit(
			report({
				platform: "windows",
				granted: true,
				screen_recording: true,
				accessibility: true,
				screen_recording_url: "",
				accessibility_url: "",
				no_gate: "Windows has no equivalent of macOS TCC",
				uipi: "higher integrity level; elevated windows discard input",
				secure_desktop: "no workaround",
				uipi_applies: true,
				secure_desktop_applies: true,
			}),
		);
		expect(cuPermissions.open).toBe(true);
		expect(cuPermissions.platform).toBe("windows");
		expect(cuPermissions.granted).toBe(true);
		expect(cuPermissions.noGate).toContain("TCC");
		expect(cuPermissions.uipi).toContain("integrity");
		expect(cuPermissions.secureDesktop).toContain("no workaround");
		expect(cuPermissions.uipiApplies).toBe(true);
	});

	it("opens on a typed uipi tool result", () => {
		startCuPermissions();
		emit({
			type: "tool_result",
			seq: 2,
			session_id: "sess-1",
			tool_call_id: "c1",
			status: "error",
			output: "UIPI discarded input",
			truncated: false,
			error_code: "uipi",
		});
		expect(cuPermissions.open).toBe(true);
		expect(cuPermissions.platform).toBe("windows");
	});

	it("opens Linux copy on a first-run linux event", () => {
		startCuPermissions();
		emit(
			report({
				platform: "linux",
				granted: true,
				screen_recording: true,
				accessibility: true,
				screen_recording_url: "",
				accessibility_url: "",
				no_gate: "X11 has no equivalent of macOS TCC",
				session_type: "x11",
			}),
		);
		expect(cuPermissions.open).toBe(true);
		expect(cuPermissions.platform).toBe("linux");
		expect(cuPermissions.granted).toBe(true);
		expect(cuPermissions.noGate).toContain("TCC");
		expect(cuPermissions.sessionType).toBe("x11");
		expect(cuPermissions.waylandApplies).toBe(false);
	});

	it("keeps Wayland limits on a linux event", () => {
		startCuPermissions();
		emit(
			report({
				platform: "linux",
				granted: false,
				screen_recording_url: "",
				accessibility_url: "",
				no_gate: "X11 has no equivalent of macOS TCC",
				session_type: "wayland",
				wayland: "This is a Wayland session",
				wayland_applies: true,
			}),
		);
		expect(cuPermissions.platform).toBe("linux");
		expect(cuPermissions.granted).toBe(false);
		expect(cuPermissions.sessionType).toBe("wayland");
		expect(cuPermissions.waylandApplies).toBe(true);
		expect(cuPermissions.wayland).toContain("Wayland");
	});

	it("opens on a typed secure_desktop tool result", () => {
		startCuPermissions();
		emit({
			type: "tool_result",
			seq: 2,
			session_id: "sess-1",
			tool_call_id: "c1",
			status: "error",
			output: "secure desktop",
			truncated: false,
			error_code: "secure_desktop",
		});
		expect(cuPermissions.open).toBe(true);
		expect(cuPermissions.platform).toBe("windows");
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
