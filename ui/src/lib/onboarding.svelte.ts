// Onboarding store (TD-1101 first-run wizard).
//
// First-run detection is keychain-grounded: the daemon's setup_state event
// carries `has_api_key` probed from the OS keychain, so "never set up"
// survives restarts and never reads plaintext config. The wizard opens when
// the first setup_state of a connection reports no key; a settings gear can
// reopen it any time (revisitable AC).
//
// Wiring: AppShell calls `start()` once on mount. The store rides the
// connection-state fan-out and probes with `get_setup_state` on every
// handshake-complete ("connected"), so reconnects re-answer the question.
// (No daemon event marks the handshake — `ready` is defined in the wire
// schema but unsent — so the client's own state transition is the trigger.)
// It sends only via connection-status's sendToDaemon — no client reference,
// no import cycle.

import { onEvent, onConnectionState, sendToDaemon } from "./connection-status.svelte.js";
import { openWorkspace } from "./session-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

export type WizardStep = "welcome" | "key" | "preset" | "workspace" | "done";
export const WIZARD_STEPS: readonly WizardStep[] = [
	"welcome",
	"key",
	"preset",
	"workspace",
	"done",
] as const;

export const onboarding = $state({
	open: false,
	step: "welcome" as WizardStep,
	hasApiKey: false,
	presets: [] as string[],
	activePreset: null as string | null,
	/** True while a validate_api_key request is in flight. */
	validating: false,
	/** Last validation verdict, cleared when a new key is stored. */
	validation: null as { ok: boolean; detail: string } | null,
});

let started = false;
/** Auto-open at most once per session lifetime; reopening is explicit. */
let autoOpened = false;

/** Register the reducer + probe once. Returns the unsubscribe for tests. */
export function start(): () => void {
	if (started) return () => {};
	started = true;
	const offEvents = onEvent(reduce);
	const offState = onConnectionState((state) => {
		// Probing after every handshake keeps the answer fresh across
		// reconnects without a new daemon round-trip elsewhere.
		if (state === "connected") sendToDaemon({ type: "get_setup_state" });
	});
	return () => {
		started = false;
		offEvents();
		offState();
	};
}

/** Reset for tests / hard reconnect flows. Does not touch the daemon. */
export function resetOnboarding(): void {
	onboarding.open = false;
	onboarding.step = "welcome";
	onboarding.hasApiKey = false;
	onboarding.presets = [];
	onboarding.activePreset = null;
	onboarding.validating = false;
	onboarding.validation = null;
	autoOpened = false;
}

function reduce(event: DaemonEventUnion): void {
	switch (event.type) {
		case "setup_state":
			onboarding.hasApiKey = event.has_api_key;
			onboarding.presets = event.presets;
			onboarding.activePreset = event.active_preset;
			// First run = no stored key. Open the wizard once; after that the
			// user drives it (settings gear or skip path).
			if (!event.has_api_key && !autoOpened) {
				autoOpened = true;
				onboarding.open = true;
				onboarding.step = "welcome";
			}
			break;
		case "api_key_validated":
			onboarding.validating = false;
			onboarding.validation = { ok: event.ok, detail: event.detail };
			if (event.ok) onboarding.hasApiKey = true;
			break;
	}
}

// ── Wizard actions ──────────────────────────────────────────────────────

export function nextStep(): void {
	const i = WIZARD_STEPS.indexOf(onboarding.step);
	if (i < WIZARD_STEPS.length - 1) onboarding.step = WIZARD_STEPS[i + 1];
}

export function backStep(): void {
	const i = WIZARD_STEPS.indexOf(onboarding.step);
	if (i > 0) onboarding.step = WIZARD_STEPS[i - 1];
}

/** Re-open the wizard (settings gear). Starts wherever makes sense. */
export function reopen(): void {
	onboarding.open = true;
	onboarding.step = "welcome";
	autoOpened = true; // don't re-auto-open over the top of it
}

/** Close without completing — every step skippable-with-consequence. */
export function closeWizard(): void {
	onboarding.open = false;
}

/** Store the pasted key; the ack (setup_state) flips hasApiKey. */
export function storeKey(apiKey: string): void {
	if (apiKey.trim().length === 0) return;
	onboarding.validation = null;
	sendToDaemon({ type: "set_api_key", api_key: apiKey.trim() });
}

/** Probe the stored key with one cheap live call. */
export function validateKey(): void {
	if (onboarding.validating) return;
	onboarding.validating = true;
	const sent = sendToDaemon({ type: "validate_api_key" });
	if (!sent) onboarding.validating = false;
}

/** Remove the stored key (TD-1102); the ack (setup_state) flips hasApiKey. */
export function removeKey(): void {
	// Any prior validation verdict is about a key that no longer exists.
	onboarding.validation = null;
	sendToDaemon({ type: "delete_api_key" });
}

/** Pick a preset; the ack (setup_state) refreshes activePreset. */
export function choosePreset(name: string): void {
	sendToDaemon({ type: "set_preset", name });
}

/** Open the picked workspace and land on done — the "go" of the wizard. */
export function chooseWorkspace(path: string): void {
	openWorkspace(path);
	onboarding.step = "done";
}

/** Finish: close the wizard; the session (if any) is already live. */
export function finish(): void {
	onboarding.open = false;
}
