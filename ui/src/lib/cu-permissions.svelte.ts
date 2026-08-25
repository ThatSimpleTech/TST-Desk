// Computer-use permission / integrity onboarding (TD-3302, TD-3303).
//
// The daemon sends `cu_permissions` on the first desktop CU attempt and
// in reply to `check_cu_permissions`. This store opens the same panel
// either way — Settings reopens the explanation; Retry re-probes.
// macOS copy is TCC. Windows copy is the missing grant dialog plus UIPI
// and the secure desktop. Linux copy is X11 no-gate honesty plus named
// Wayland / XTEST / display limits. The pane branches on `platform`.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { isTauri } from "./open-file";
import type { CuPermissions, DaemonEventUnion } from "./protocol";

const WINDOWS_REFUSE = new Set(["uipi", "secure_desktop"]);

export const cuPermissions = $state({
	open: false,
	granted: false,
	screenRecording: false,
	accessibility: false,
	screenRecordingUrl: "",
	accessibilityUrl: "",
	firstRun: false,
	probing: false,
	platform: "" as "" | "macos" | "windows" | "linux",
	noGate: "",
	uipi: "",
	secureDesktop: "",
	elevated: false,
	uipiApplies: false,
	secureDesktopApplies: false,
	sessionType: "",
	wayland: "",
	waylandApplies: false,
	xtest: "",
	xtestApplies: false,
	noDisplay: "",
	noDisplayApplies: false,
});

let started = false;

/** Register the reducer. Unsubscribe for tests. */
export function startCuPermissions(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetCuPermissions(): void {
	cuPermissions.open = false;
	cuPermissions.granted = false;
	cuPermissions.screenRecording = false;
	cuPermissions.accessibility = false;
	cuPermissions.screenRecordingUrl = "";
	cuPermissions.accessibilityUrl = "";
	cuPermissions.firstRun = false;
	cuPermissions.probing = false;
	cuPermissions.platform = "";
	cuPermissions.noGate = "";
	cuPermissions.uipi = "";
	cuPermissions.secureDesktop = "";
	cuPermissions.elevated = false;
	cuPermissions.uipiApplies = false;
	cuPermissions.secureDesktopApplies = false;
	cuPermissions.sessionType = "";
	cuPermissions.wayland = "";
	cuPermissions.waylandApplies = false;
	cuPermissions.xtest = "";
	cuPermissions.xtestApplies = false;
	cuPermissions.noDisplay = "";
	cuPermissions.noDisplayApplies = false;
	started = false;
}

function applyReport(event: CuPermissions): void {
	cuPermissions.granted = event.granted;
	cuPermissions.screenRecording = event.screen_recording;
	cuPermissions.accessibility = event.accessibility;
	cuPermissions.screenRecordingUrl = event.screen_recording_url;
	cuPermissions.accessibilityUrl = event.accessibility_url;
	cuPermissions.firstRun = event.first_run;
	cuPermissions.platform = event.platform;
	cuPermissions.noGate = event.no_gate;
	cuPermissions.uipi = event.uipi;
	cuPermissions.secureDesktop = event.secure_desktop;
	cuPermissions.elevated = event.elevated;
	cuPermissions.uipiApplies = event.uipi_applies;
	cuPermissions.secureDesktopApplies = event.secure_desktop_applies;
	cuPermissions.sessionType = event.session_type;
	cuPermissions.wayland = event.wayland;
	cuPermissions.waylandApplies = event.wayland_applies;
	cuPermissions.xtest = event.xtest;
	cuPermissions.xtestApplies = event.xtest_applies;
	cuPermissions.noDisplay = event.no_display;
	cuPermissions.noDisplayApplies = event.no_display_applies;
	cuPermissions.probing = false;
	cuPermissions.open = true;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "cu_permissions") {
		applyReport(event);
		return;
	}
	// First deny still opens the panel if the connection event is late —
	// the tool result is the typed error the daemon already sent.
	if (event.type === "tool_result" && event.error_code === "permission_denied") {
		cuPermissions.open = true;
	}
	if (event.type === "tool_result" && event.error_code && WINDOWS_REFUSE.has(event.error_code)) {
		cuPermissions.open = true;
		cuPermissions.platform = "windows";
	}
}

/** Settings / wizard: ask the daemon and open the same copy. */
export function openCuPermissions(): void {
	cuPermissions.open = true;
	cuPermissions.probing = true;
	const sent = sendToDaemon({ type: "check_cu_permissions" });
	if (!sent) cuPermissions.probing = false;
}

/** Retry: re-probe without waiting on TCC or a grant dialog that does not exist. */
export function retryCuPermissions(): void {
	cuPermissions.probing = true;
	const sent = sendToDaemon({ type: "check_cu_permissions" });
	if (!sent) cuPermissions.probing = false;
}

export function closeCuPermissions(): void {
	cuPermissions.open = false;
}

/** Open a System Settings deep link. Best-effort outside the Tauri shell. */
export async function openSystemSettings(url: string): Promise<boolean> {
	if (url === "") return false;
	if (!isTauri()) {
		if (typeof window === "undefined") return false;
		window.open(url);
		return true;
	}
	try {
		const { openUrl } = await import("@tauri-apps/plugin-opener");
		await openUrl(url);
		return true;
	} catch {
		return false;
	}
}
