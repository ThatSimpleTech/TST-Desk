// TD-5200: Settings → API keys keeps each saved key's Test result on its own
// row. These cover the store: one result per credential, the in-flight state,
// giving up after 30 seconds, what drops a result, and the first-run check
// (which shares api_key_validated) left alone.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
	ClientMessageUnion,
	CredentialSummary,
	DaemonEventUnion,
	SetupState,
} from "./protocol";

const mocks = vi.hoisted(() => ({
	handlers: [] as ((e: DaemonEventUnion) => void)[],
	sent: [] as ClientMessageUnion[],
	sendOk: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.push(handler);
		return () => {
			mocks.handlers = mocks.handlers.filter((h) => h !== handler);
		};
	},
	onConnectionState: () => () => {},
	sendToDaemon: (msg: ClientMessageUnion) => {
		if (!mocks.sendOk) return false;
		mocks.sent.push(msg);
		return true;
	},
}));

vi.mock("./session-status.svelte.js", () => ({ openWorkspace: () => {} }));

import { KEY_TEST_COPY, KEY_TEST_TIMEOUT_MS, keyTests, testKey } from "./key-tests.svelte.js";
import {
	onboarding,
	resetOnboarding,
	start as startOnboarding,
	validateKey,
} from "./onboarding.svelte.js";
import { resetSettings, startSettings } from "./settings.svelte.js";

const TOWER: CredentialSummary = {
	id: "tower",
	name: "Tower",
	stored: true,
	base_url: "http://127.0.0.1:8000/v1",
};
const SPARE: CredentialSummary = { id: "spare", name: "Spare", stored: true, base_url: null };

let stopOnboarding: (() => void) | null = null;

function emit(event: DaemonEventUnion): void {
	for (const handler of [...mocks.handlers]) handler(event);
}

function setupState(
	credentials: CredentialSummary[] = [TOWER, SPARE],
	over: Partial<SetupState> = {},
): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["main", "other"],
		active_preset: "main",
		credentials,
		tier_credentials: { brain: "tower", worker: null, validator: null },
		tier_loopback: { brain: false, worker: false, validator: true },
		...over,
	};
}

function verdict(credential: string | null, ok: boolean, detail: string): DaemonEventUnion {
	return { type: "api_key_validated", seq: 1, ok, detail, credential };
}

function validates(): ClientMessageUnion[] {
	return mocks.sent.filter((m) => m.type === "validate_api_key");
}

beforeEach(() => {
	mocks.handlers = [];
	mocks.sent = [];
	mocks.sendOk = true;
	resetSettings();
	resetOnboarding();
	startSettings();
	emit(setupState());
});

afterEach(() => {
	stopOnboarding?.();
	stopOnboarding = null;
	vi.useRealTimers();
	resetSettings();
	resetOnboarding();
});

describe("a row's Test (TD-5200)", () => {
	it("asks for that key once and shows it is testing", () => {
		testKey("tower");
		expect(validates()).toEqual([{ type: "validate_api_key", credential: "tower" }]);
		expect(keyTests.tower).toEqual({ state: "testing" });

		testKey("tower");
		expect(validates()).toHaveLength(1);
	});

	it("shows the verdict on the row that asked, with the daemon's words", () => {
		testKey("tower");
		emit(verdict("tower", true, "Key accepted by http://127.0.0.1:8000/v1."));
		expect(keyTests.tower).toEqual({
			state: "ok",
			detail: "Key accepted by http://127.0.0.1:8000/v1.",
		});

		testKey("tower");
		expect(keyTests.tower).toEqual({ state: "testing" });
		emit(verdict("tower", false, "Authentication failed."));
		expect(keyTests.tower).toEqual({ state: "failed", detail: "Authentication failed." });
	});

	it("says the key was rejected when a failure carries no reason", () => {
		testKey("tower");
		emit(verdict("tower", false, ""));
		expect(keyTests.tower).toEqual({ state: "failed", detail: KEY_TEST_COPY.rejected });
	});

	it("keeps each key's result to itself", () => {
		testKey("tower");
		testKey("spare");
		emit(verdict("spare", false, "Spare refused."));
		expect(keyTests.tower).toEqual({ state: "testing" });
		expect(keyTests.spare).toEqual({ state: "failed", detail: "Spare refused." });

		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
		expect(keyTests.spare).toEqual({ state: "failed", detail: "Spare refused." });
	});

	it("ignores a verdict for a key nobody tested here", () => {
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toBeUndefined();
	});
});

describe("no answer", () => {
	it("gives up after 30 seconds and lets the row test again", () => {
		vi.useFakeTimers();
		testKey("tower");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS - 1);
		expect(keyTests.tower).toEqual({ state: "testing" });
		vi.advanceTimersByTime(1);
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.noAnswer });
		expect(KEY_TEST_COPY.noAnswer).toContain("30 seconds");

		testKey("tower");
		expect(validates()).toHaveLength(2);
		expect(keyTests.tower).toEqual({ state: "testing" });
	});

	it("still shows a verdict that arrives after the row gave up", () => {
		vi.useFakeTimers();
		testKey("tower");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
	});

	it("an answered test does not give up later", () => {
		vi.useFakeTimers();
		testKey("tower");
		emit(verdict("tower", true, "Key accepted."));
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS * 2);
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
	});

	it("says so at once when the request could not be sent", () => {
		vi.useFakeTimers();
		mocks.sendOk = false;
		testKey("tower");
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.notConnected });
		expect(vi.getTimerCount()).toBe(0);
	});
});

describe("what drops a result", () => {
	function answered(): void {
		testKey("tower");
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower?.state).toBe("ok");
	}

	it("keeps it while the key's name, host and stored state are unchanged", () => {
		answered();
		emit(setupState([{ ...TOWER }, { ...SPARE, name: "Spare renamed" }]));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
	});

	it.each([
		["a new name", { ...TOWER, name: "Tower 2" }],
		["a new host", { ...TOWER, base_url: "http://127.0.0.1:9000/v1" }],
		["no host", { ...TOWER, base_url: null }],
		["the key removed", { ...TOWER, stored: false }],
	])("drops it on %s", (_why, changed) => {
		answered();
		emit(setupState([changed, SPARE]));
		expect(keyTests.tower).toBeUndefined();
	});

	it("drops it when the row is gone", () => {
		answered();
		emit(setupState([SPARE]));
		expect(keyTests.tower).toBeUndefined();
	});

	// The daemon tests a key against a tier that sends it (key_probe), so a
	// result is also about which tiers send the key and the active preset.
	it.each([
		["a tier bound to the key", { tier_credentials: { brain: "tower", worker: "tower" } }],
		["its tier bound to another key", { tier_credentials: { brain: "spare" } }],
		["a preset switch", { active_preset: "other" }],
	])("drops it on %s", (_why, over) => {
		answered();
		emit(setupState([TOWER, SPARE], over));
		expect(keyTests.tower).toBeUndefined();
	});

	it("drops 'no model uses this key' once a tier is bound to it", () => {
		testKey("spare");
		const nowhere = "No model uses this key yet, so there is nothing to test it against.";
		emit(verdict("spare", false, nowhere));
		expect(keyTests.spare?.state).toBe("failed");
		emit(setupState([TOWER, SPARE], { tier_credentials: { brain: "tower", worker: "spare" } }));
		expect(keyTests.spare).toBeUndefined();
	});

	it("keeps it when only another key's tiers change", () => {
		answered();
		testKey("spare");
		emit(verdict("spare", true, "Key accepted."));
		emit(setupState([TOWER, SPARE], { tier_credentials: { brain: "tower", validator: "spare" } }));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
		expect(keyTests.spare).toBeUndefined();
	});

	it("follows the default key to the unbound remote tiers that send it", () => {
		const openrouter: CredentialSummary = { id: "openrouter", name: "OpenRouter", stored: true };
		emit(setupState([openrouter, TOWER]));
		testKey("openrouter");
		emit(verdict("openrouter", true, "Key accepted by the active preset's provider."));
		// The worker moves to a server on this computer: it sends no key now.
		emit(
			setupState([openrouter, TOWER], {
				tier_loopback: { brain: false, worker: true, validator: true },
			}),
		);
		expect(keyTests.openrouter).toBeUndefined();
	});

	it("drops a no-answer too, and a late verdict about the old host stays off the row", () => {
		vi.useFakeTimers();
		testKey("tower");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		emit(setupState([{ ...TOWER, base_url: "http://127.0.0.1:9000/v1" }, SPARE]));
		expect(keyTests.tower).toBeUndefined();
		emit(verdict("tower", true, "Key accepted by http://127.0.0.1:8000/v1."));
		expect(keyTests.tower).toBeUndefined();
	});

	it("a host saved on the way to Test is the host the verdict is about", () => {
		// Blur commits the host, then the click tests: the daemon acks the host
		// before it answers the test, and the result must survive that ack.
		testKey("tower");
		const moved = { ...TOWER, base_url: "http://127.0.0.1:9000/v1" };
		emit(setupState([moved, SPARE]));
		expect(keyTests.tower).toEqual({ state: "testing" });
		emit(verdict("tower", true, "Key accepted by http://127.0.0.1:9000/v1."));
		expect(keyTests.tower?.state).toBe("ok");
		emit(setupState([moved, SPARE]));
		expect(keyTests.tower?.state).toBe("ok");
	});

	it("a row removed while its test runs shows nothing when the verdict lands", () => {
		testKey("tower");
		emit(setupState([SPARE]));
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toBeUndefined();
	});
});

describe("the first-run check shares the event", () => {
	it("its verdict (no key named) stays off every row, and a row's stays off it", () => {
		stopOnboarding = startOnboarding();
		testKey("tower");
		validateKey("sk-typed-first-run");
		expect(onboarding.validating).toBe(true);

		emit(verdict(null, false, "Failed to connect."));
		expect(keyTests.tower).toEqual({ state: "testing" });
		expect(onboarding.validating).toBe(false);
		expect(onboarding.validation).toEqual({ ok: false, detail: "Failed to connect." });

		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
		expect(onboarding.validation).toEqual({ ok: false, detail: "Failed to connect." });
	});
});
