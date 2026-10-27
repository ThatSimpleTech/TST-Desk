// Onboarding store (TD-1101 first-run wizard, TD-5172 "Add a model").
//
// First-run detection comes from the daemon: setup_state's `has_model` is
// false when the config has no model yet (nothing ships), so "never set up"
// survives restarts and never reads plaintext config. The wizard opens on
// the first such setup_state of a connection; the picker, Settings and the
// gear can reopen it any time (revisitable AC).
//
// The "Add a model" step walks: server URL → key → pick from the server's
// /models → label and description → one test message. The typed key lives
// only in this store while the step is open; the daemon puts it in the OS
// keychain and the store drops it.
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
import type { DaemonEventUnion, EndpointModel, LocalServer } from "./protocol";

export type WizardStep = "welcome" | "model" | "workspace" | "done";
export const WIZARD_STEPS: readonly WizardStep[] = ["welcome", "model", "workspace", "done"] as const;

/** The parts of the "Add a model" step, in order. */
export type ModelStep = "url" | "key" | "pick" | "label" | "test";
export const MODEL_STEPS: readonly ModelStep[] = ["url", "key", "pick", "label", "test"] as const;

function blankModelForm() {
	return {
		sub: "url" as ModelStep,
		baseUrl: "",
		/** The typed key, held only until add_model sends it. */
		apiKey: "",
		listing: false,
		models: [] as EndpointModel[],
		listError: "",
		/** "Find models on this computer" (TD-5180): in flight, then its answer. */
		scanning: false,
		scan: null as { servers: LocalServer[]; checked: string[] } | null,
		slug: "",
		label: "",
		description: "",
		contextWindow: null as number | null,
		inputPrice: null as number | null,
		outputPrice: null as number | null,
		adding: false,
		addError: "",
		/** The preset add_model wrote (the active one after the ack). */
		added: "",
		testing: false,
		test: null as { ok: boolean; reply: string; detail: string } | null,
	};
}

/** True for a URL on this computer, which needs no key. */
export function isLocalUrl(url: string): boolean {
	try {
		const host = new URL(url.trim()).hostname.replace(/^\[|\]$/g, "");
		return host === "localhost" || host === "::1" || host.startsWith("127.");
	} catch {
		return false;
	}
}

/** A first guess at a label from a model id: `vendor/model-a` → `model-a`. */
export function labelFromSlug(slug: string): string {
	const tail = slug.split("/").pop() ?? slug;
	return tail.slice(0, 40);
}

export const onboarding = $state({
	open: false,
	step: "welcome" as WizardStep,
	hasApiKey: false,
	/** False until the config has a model; the first run starts there. */
	hasModel: true,
	presets: [] as string[],
	activePreset: null as string | null,
	/** True while a validate_api_key request is in flight. */
	validating: false,
	/** Last validation verdict, cleared when a new key is stored. */
	validation: null as { ok: boolean; detail: string } | null,
	/** The "Add a model" form. */
	model: blankModelForm(),
	/** Opened for "Add a model" alone (picker, Settings): done closes it. */
	addOnly: false,
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

// Whether the in-flight validate covers a typed key (TD-1106): an ok
// verdict for a typed key must not flip hasApiKey — nothing was stored.
let validateWasTyped = false;

/** Reset for tests / hard reconnect flows. Does not touch the daemon. */
export function resetOnboarding(): void {
	onboarding.open = false;
	onboarding.step = "welcome";
	onboarding.hasApiKey = false;
	onboarding.hasModel = true;
	onboarding.presets = [];
	onboarding.activePreset = null;
	onboarding.validating = false;
	onboarding.validation = null;
	onboarding.model = blankModelForm();
	onboarding.addOnly = false;
	autoOpened = false;
	validateWasTyped = false;
}

function reduce(event: DaemonEventUnion): void {
	switch (event.type) {
		case "setup_state":
			onboarding.hasApiKey = event.has_api_key;
			onboarding.hasModel = event.has_model ?? true;
			onboarding.presets = event.presets;
			onboarding.activePreset = event.active_preset;
			// setup_state is add_model's ack: the new preset is now active.
			if (onboarding.model.adding) {
				onboarding.model.adding = false;
				onboarding.model.added = event.active_preset;
				onboarding.model.apiKey = "";
				onboarding.model.sub = "test";
				testModel();
			}
			// First run = no model set up (TD-5172). Open the wizard once;
			// after that the user drives it (picker, Settings or the gear).
			if (event.has_model === false && !autoOpened) {
				autoOpened = true;
				onboarding.open = true;
				onboarding.step = "welcome";
			}
			break;
		case "endpoint_models":
			if (!onboarding.model.listing) break;
			onboarding.model.listing = false;
			onboarding.model.models = event.models;
			onboarding.model.listError = event.error ?? "";
			if (!event.error && event.models.length > 0) onboarding.model.sub = "pick";
			break;
		case "local_servers":
			if (!onboarding.model.scanning) break;
			onboarding.model.scanning = false;
			onboarding.model.scan = { servers: event.servers, checked: event.checked };
			break;
		case "model_tested":
			onboarding.model.testing = false;
			onboarding.model.test = { ok: event.ok, reply: event.reply, detail: event.detail };
			break;
		case "error":
			if (onboarding.model.adding) {
				onboarding.model.adding = false;
				onboarding.model.addError = event.message;
			}
			if (onboarding.model.scanning && event.in_reply_to === "scan_local_servers") {
				onboarding.model.scanning = false;
				onboarding.model.listError = event.message;
			}
			break;
		case "api_key_validated":
			onboarding.validating = false;
			onboarding.validation = { ok: event.ok, detail: event.detail };
			if (event.ok && !validateWasTyped) onboarding.hasApiKey = true;
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
	onboarding.addOnly = false;
	onboarding.model = blankModelForm();
	autoOpened = true; // don't re-auto-open over the top of it
}

/** Open straight on "Add a model" (the picker and Settings → Models). */
export function openAddModel(): void {
	onboarding.model = blankModelForm();
	onboarding.addOnly = true;
	onboarding.open = true;
	onboarding.step = "model";
	autoOpened = true;
}

// ── "Add a model" (TD-5172) ─────────────────────────────────────────────

/** Step 1: the server's API URL. A server on this computer skips the key. */
export function setModelUrl(url: string): void {
	const form = onboarding.model;
	form.baseUrl = url.trim();
	form.listError = "";
	if (isLocalUrl(form.baseUrl)) {
		form.apiKey = "";
		listModels();
	} else {
		form.sub = "key";
	}
}

/** "Find models on this computer" (TD-5180): only ever on the user's click. */
export function scanLocalServers(): void {
	const form = onboarding.model;
	if (form.scanning) return;
	form.scanning = true;
	form.scan = null;
	form.listError = "";
	if (!sendToDaemon({ type: "scan_local_servers" })) {
		form.scanning = false;
		form.listError = "TST Desk is not connected. Try again in a moment.";
	}
}

/** Take a server the scan found: on this computer, so no key; pick next. */
export function chooseLocalServer(server: LocalServer): void {
	const form = onboarding.model;
	form.baseUrl = server.base_url;
	form.apiKey = "";
	form.listError = "";
	form.models = server.models;
	form.sub = "pick";
}

/** Step 2: ask the server what it offers, with the typed key if any. */
export function listModels(apiKey?: string): void {
	const form = onboarding.model;
	if (form.listing || form.baseUrl === "") return;
	if (apiKey !== undefined) form.apiKey = apiKey.trim();
	form.listing = true;
	form.listError = "";
	const sent = sendToDaemon({
		type: "list_endpoint_models",
		base_url: form.baseUrl,
		...(form.apiKey ? { api_key: form.apiKey } : {}),
	});
	if (!sent) {
		form.listing = false;
		form.listError = "TST Desk is not connected. Try again in a moment.";
	}
}

/** Step 3: pick one model from the list. */
export function pickModel(row: EndpointModel): void {
	const form = onboarding.model;
	form.slug = row.id;
	form.contextWindow = row.context_window ?? null;
	form.inputPrice = row.input_price ?? null;
	form.outputPrice = row.output_price ?? null;
	if (form.label === "") form.label = labelFromSlug(row.id);
	form.sub = "label";
}

/** Step 4: save it under a label; the setup_state ack runs the test. */
export function addModel(label: string, description: string): void {
	const form = onboarding.model;
	if (form.adding || label.trim() === "" || form.slug === "") return;
	form.label = label.trim();
	form.description = description.trim();
	form.addError = "";
	form.adding = true;
	const sent = sendToDaemon({
		type: "add_model",
		label: form.label,
		description: form.description,
		base_url: form.baseUrl,
		slug: form.slug,
		...(form.apiKey ? { api_key: form.apiKey } : {}),
		...(form.contextWindow !== null ? { context_window: form.contextWindow } : {}),
		...(form.inputPrice !== null ? { input_price: form.inputPrice } : {}),
		...(form.outputPrice !== null ? { output_price: form.outputPrice } : {}),
	});
	if (!sent) {
		form.adding = false;
		form.addError = "TST Desk is not connected. Try again in a moment.";
	}
}

/** Step 5: one short message to the new model. */
export function testModel(): void {
	const form = onboarding.model;
	if (form.testing) return;
	form.testing = true;
	form.test = null;
	const sent = sendToDaemon(
		form.added ? { type: "test_model", preset: form.added } : { type: "test_model" },
	);
	if (!sent) form.testing = false;
}

/** Back one part of the "Add a model" step; from the first, leave it. */
export function backModelStep(): void {
	const form = onboarding.model;
	if (form.sub === "url") {
		backStep();
		return;
	}
	if (form.sub === "pick" && isLocalUrl(form.baseUrl)) {
		form.sub = "url";
		return;
	}
	const i = MODEL_STEPS.indexOf(form.sub);
	form.sub = MODEL_STEPS[Math.max(0, i - 1)];
}

/** Close without completing — every step skippable-with-consequence. */
export function closeWizard(): void {
	onboarding.open = false;
	onboarding.model.apiKey = ""; // a typed key never outlives the wizard
}

/** Store the pasted key; the ack (setup_state) flips hasApiKey. */
export function storeKey(apiKey: string): void {
	if (apiKey.trim().length === 0) return;
	onboarding.validation = null;
	sendToDaemon({ type: "set_api_key", api_key: apiKey.trim() });
}

/** Probe a key with one cheap live call (TD-1106).

With *apiKey*, the key currently typed in the field is checked directly —
independent of any stored key, so a failed or skipped store never blocks
validation.  Without it, the stored key is probed.
*/
export function validateKey(apiKey?: string): void {
	if (onboarding.validating) return;
	const typed = apiKey?.trim();
	if (!typed && !onboarding.hasApiKey) return; // nothing to validate
	onboarding.validating = true;
	validateWasTyped = !!typed;
	const sent = sendToDaemon(
		typed ? { type: "validate_api_key", api_key: typed } : { type: "validate_api_key" }
	);
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
	onboarding.model = blankModelForm();
}
