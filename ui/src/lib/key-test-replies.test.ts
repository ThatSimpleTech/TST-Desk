// TD-5200 review fixes: which request a key test's reply answers. The daemon
// answers one connection's messages in order, so the store ties each reply
// to the oldest test still waiting. These cover a row tested again while its
// first test is still out, the 30-second clock for a test queued behind
// another, a refused test, a dropped connection, an older daemon's verdict
// that names no key, and a window that reaches another computer.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
	ClientMessageUnion,
	CredentialSummary,
	DaemonEventUnion,
	HelloAck,
	SetupState,
} from "./protocol";

const mocks = vi.hoisted(() => ({
	handlers: [] as ((e: DaemonEventUnion) => void)[],
	states: [] as ((state: string) => void)[],
	sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.push(handler);
		return () => {
			mocks.handlers = mocks.handlers.filter((h) => h !== handler);
		};
	},
	onConnectionState: (handler: (state: string) => void) => {
		mocks.states.push(handler);
		return () => {
			mocks.states = mocks.states.filter((h) => h !== handler);
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

vi.mock("./session-status.svelte.js", () => ({ openWorkspace: () => {} }));

import { noteHandshake, resetHub } from "./hub.svelte.js";
import { KEY_TEST_COPY, KEY_TEST_TIMEOUT_MS, keyTests, testKey } from "./key-tests.svelte.js";
import {
	onboarding,
	resetOnboarding,
	start as startOnboarding,
	validateKey,
} from "./onboarding.svelte.js";
import { resetSettings, startSettings } from "./settings.svelte.js";

const OLD_HOST = "http://127.0.0.1:8000/v1";
const NEW_HOST = "http://127.0.0.1:9000/v1";
const TOWER: CredentialSummary = { id: "tower", name: "Tower", stored: true, base_url: OLD_HOST };
const SPARE: CredentialSummary = { id: "spare", name: "Spare", stored: true, base_url: null };

let stopOnboarding: (() => void) | null = null;

function emit(event: DaemonEventUnion): void {
	for (const handler of [...mocks.handlers]) handler(event);
}

function connection(state: string): void {
	for (const handler of [...mocks.states]) handler(state);
}

function setupState(credentials: CredentialSummary[] = [TOWER, SPARE]): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["main"],
		active_preset: "main",
		credentials,
	};
}

function verdict(credential: string | null, ok: boolean, detail: string): DaemonEventUnion {
	return { type: "api_key_validated", seq: 1, ok, detail, credential };
}

/** A verdict from a daemon older than TD-5200: no `credential` field at all. */
function unnamed(ok: boolean, detail: string): DaemonEventUnion {
	return { type: "api_key_validated", seq: 1, ok, detail };
}

function handshake(machineId: string): void {
	noteHandshake({ type: "hello_ack", version: 1, machine_id: machineId } as HelloAck);
}

beforeEach(() => {
	mocks.handlers = [];
	mocks.states = [];
	mocks.sent = [];
	resetHub();
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
	resetHub();
});

describe("a row tested again while its first test is still out", () => {
	it("shows the second test's verdict, not the first's late one", () => {
		vi.useFakeTimers();
		testKey("tower");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.noAnswer });

		// The host is fixed, saved by leaving the field, and tested again. The
		// daemon answers in order: the first test, the host's ack, the second.
		testKey("tower");
		emit(verdict("tower", false, `Failed to connect to ${OLD_HOST}.`));
		expect(keyTests.tower).toEqual({ state: "testing" });
		emit(setupState([{ ...TOWER, base_url: NEW_HOST }, SPARE]));
		expect(keyTests.tower).toEqual({ state: "testing" });
		emit(verdict("tower", true, `Key accepted by ${NEW_HOST}.`));
		expect(keyTests.tower).toEqual({ state: "ok", detail: `Key accepted by ${NEW_HOST}.` });
	});

	it("starts the second test's clock only when the first is answered", () => {
		vi.useFakeTimers();
		testKey("tower");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		testKey("tower");
		// The first test still holds the daemon, so the second has not started.
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS * 3);
		expect(keyTests.tower).toEqual({ state: "testing" });

		emit(verdict("tower", false, "The first test's answer."));
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS - 1);
		expect(keyTests.tower).toEqual({ state: "testing" });
		vi.advanceTimersByTime(1);
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.noAnswer });
	});
});

describe("a test queued behind another row's", () => {
	it("is not blamed for the wait, and gets its own 30 seconds", () => {
		vi.useFakeTimers();
		testKey("tower");
		testKey("spare");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.noAnswer });
		expect(keyTests.spare).toEqual({ state: "testing" });

		emit(verdict("tower", false, `No answer from ${OLD_HOST} within 20 seconds.`));
		expect(keyTests.tower?.state).toBe("failed");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS - 1);
		expect(keyTests.spare).toEqual({ state: "testing" });
		emit(verdict("spare", true, "Key accepted."));
		expect(keyTests.spare).toEqual({ state: "ok", detail: "Key accepted." });
	});

	it("an error in reply to a test answers it in turn", () => {
		vi.useFakeTimers();
		testKey("tower");
		testKey("spare");
		emit({
			type: "error",
			seq: 0,
			code: "bad_request",
			message: "That request was refused.",
			in_reply_to: "validate_api_key",
		} as DaemonEventUnion);
		expect(keyTests.tower).toEqual({ state: "failed", detail: "That request was refused." });
		expect(keyTests.spare).toEqual({ state: "testing" });
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		expect(keyTests.spare?.state).toBe("no_answer");
	});

	it("an error about something else leaves the tests waiting", () => {
		testKey("tower");
		emit({
			type: "error",
			seq: 0,
			code: "bad_request",
			message: "No.",
			in_reply_to: "set_credential",
		} as DaemonEventUnion);
		expect(keyTests.tower).toEqual({ state: "testing" });
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower?.state).toBe("ok");
	});
});

describe("a dropped connection", () => {
	it("ends every test still waiting, and the next test starts afresh", () => {
		vi.useFakeTimers();
		testKey("tower");
		testKey("spare");
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		connection("reconnecting");
		expect(keyTests.tower).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.dropped });
		expect(keyTests.spare).toEqual({ state: "no_answer", detail: KEY_TEST_COPY.dropped });
		expect(vi.getTimerCount()).toBe(0);

		connection("connected");
		testKey("tower");
		emit(verdict("tower", true, "Key accepted."));
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
	});

	it("keeps a settled result across a reconnect to the same computer", () => {
		handshake("mid_aaaaaaaaaaaaaaaa");
		testKey("tower");
		emit(verdict("tower", true, "Key accepted."));
		connection("reconnecting");
		connection("connected");
		emit(setupState());
		expect(keyTests.tower).toEqual({ state: "ok", detail: "Key accepted." });
	});
});

describe("another computer", () => {
	it("drops every result when the window reaches a different computer", () => {
		handshake("mid_aaaaaaaaaaaaaaaa");
		testKey("tower");
		emit(verdict("tower", true, "Key accepted."));

		// A phone paired with another computer reconnects in place.
		connection("stopped");
		handshake("mid_bbbbbbbbbbbbbbbb");
		connection("connected");
		emit(setupState());
		expect(keyTests.tower).toBeUndefined();
	});
});

describe("an older daemon's verdict, which names no key", () => {
	it("lands on the row that asked, and the first-run check keeps its own", () => {
		vi.useFakeTimers();
		stopOnboarding = startOnboarding();
		testKey("tower");
		emit(unnamed(true, "Key accepted by the active preset's provider."));
		expect(keyTests.tower).toEqual({
			state: "ok",
			detail: "Key accepted by the active preset's provider.",
		});
		expect(onboarding.validation).toBeNull();
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS * 2);
		expect(keyTests.tower?.state).toBe("ok");
	});

	it.each(["settings", "onboarding"])(
		"goes to whoever asked first, whichever store sees it first (%s)",
		(first) => {
			if (first === "onboarding") {
				// The app starts the onboarding store before the settings one.
				mocks.handlers = [];
				resetSettings();
				stopOnboarding = startOnboarding();
				startSettings();
				emit(setupState());
			} else {
				stopOnboarding = startOnboarding();
			}
			validateKey("sk-typed-first-run");
			testKey("tower");
			testKey("spare");

			emit(unnamed(false, "The first-run check's answer."));
			expect(onboarding.validating).toBe(false);
			expect(onboarding.validation).toEqual({ ok: false, detail: "The first-run check's answer." });
			expect(keyTests.tower).toEqual({ state: "testing" });

			emit(unnamed(true, "Tower's answer."));
			expect(keyTests.tower).toEqual({ state: "ok", detail: "Tower's answer." });
			expect(onboarding.validation).toEqual({ ok: false, detail: "The first-run check's answer." });

			emit(unnamed(false, "Spare's answer."));
			expect(keyTests.spare).toEqual({ state: "failed", detail: "Spare's answer." });
			expect(keyTests.tower).toEqual({ state: "ok", detail: "Tower's answer." });
		},
	);
});
