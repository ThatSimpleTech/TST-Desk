// @vitest-environment jsdom
//
// TD-4838: API-key fields must not look like an sk- format requirement,
// and a failed Test has to show the daemon's reason.
// TD-5200: Test shows its in-flight state and its verdict on the row it was
// pressed on, and nowhere else.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, SetupState } from "./protocol";

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
	onConnectionState: () => () => {},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import SettingsKeys from "./components/SettingsKeys.svelte";
import { KEY_TEST_COPY, KEY_TEST_TIMEOUT_MS } from "./key-tests.svelte.js";
import { resetSettings, startSettings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function setupState(over: Partial<SetupState> = {}): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["local"],
		active_preset: "local",
		tier_slugs: { brain: null, worker: null, validator: null },
		credentials: [
			{ id: "tower", name: "TOWER", stored: true },
			{ id: "local", name: "Local", stored: false },
		],
		...over,
	};
}

const TWO_STORED = setupState({
	credentials: [
		{ id: "tower", name: "TOWER", stored: true },
		{ id: "spare", name: "Spare", stored: true, base_url: "http://127.0.0.1:8000/v1" },
	],
});

function verdict(credential: string | null, ok: boolean, detail: string): DaemonEventUnion {
	return { type: "api_key_validated", seq: 1, ok, detail, credential };
}

function button(name: string, index: number): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].filter(
		(el) => el.textContent?.trim() === name,
	)[index];
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

function row(index: number): HTMLElement {
	const found = document.body.querySelectorAll<HTMLElement>(".row")[index];
	if (found === undefined) throw new Error(`missing row ${index}`);
	return found;
}

/** The row's Test button, whatever it says right now. */
function testButton(index: number): HTMLButtonElement {
	const found = [...row(index).querySelectorAll("button")].find((el) =>
		el.textContent?.trim().startsWith("Test"),
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`no Test button on row ${index}`);
	return found;
}

/** The row's live region, where its verdict is read out. */
function result(index: number): HTMLElement {
	const found = row(index).querySelector<HTMLElement>('[role="status"]');
	if (found === null) throw new Error(`no result region on row ${index}`);
	return found;
}

function text(el: Element): string {
	return (el.textContent ?? "").replace(/\s+/g, " ").trim();
}

async function mountWith(state: SetupState): Promise<void> {
	emit(state);
	app = mount(SettingsKeys, { target: document.body });
	await tick();
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	resetSettings();
	startSettings();
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	vi.useRealTimers();
	document.body.replaceChildren();
	resetSettings();
});

describe("API key fields (TD-4838)", () => {
	it("uses a neutral placeholder and enables Test when a key is stored", async () => {
		await mountWith(setupState());

		const placeholders = [...document.body.querySelectorAll("input")].map((el) =>
			el.getAttribute("placeholder"),
		);
		expect(placeholders).toContain("Paste API key");
		expect(placeholders.some((value) => value?.startsWith("sk-"))).toBe(false);

		expect(button("Test", 0).disabled).toBe(false);
		expect(button("Test", 1).disabled).toBe(true);
		button("Test", 0).click();
		expect(mocks.sent.at(-1)).toEqual({ type: "validate_api_key", credential: "tower" });
	});

	it("shows the daemon reason on the row when Test fails", async () => {
		await mountWith(setupState());
		testButton(0).click();
		await tick();
		emit(verdict("tower", false, "Re-save the key in Settings → API keys"));
		await tick();
		expect(text(result(0))).toBe("Not connected Re-save the key in Settings → API keys");

		testButton(0).click();
		await tick();
		emit(verdict("tower", false, ""));
		await tick();
		expect(text(result(0))).toBe(`Not connected ${KEY_TEST_COPY.rejected}`);
	});
});

describe("Test shows its result on the row (TD-5200)", () => {
	it("says Testing… with the button off, then Connected with the daemon's words", async () => {
		await mountWith(setupState());
		const region = result(0);
		expect(region.getAttribute("aria-live")).toBe("polite");
		expect(text(region)).toBe("");

		testButton(0).click();
		await tick();
		expect(text(testButton(0))).toBe("Testing…");
		expect(testButton(0).disabled).toBe(true);

		emit(verdict("tower", true, "Key accepted by http://127.0.0.1:8000/v1."));
		await tick();
		expect(text(testButton(0))).toBe("Test");
		expect(testButton(0).disabled).toBe(false);
		// The same region the reader was watching now holds the verdict.
		expect(result(0)).toBe(region);
		expect(text(region)).toBe("Connected Key accepted by http://127.0.0.1:8000/v1.");
		expect(region.querySelector(".verdict--ok svg")).not.toBeNull();
		expect(text(result(1))).toBe("");
	});

	it("keeps each row's result on its own row", async () => {
		await mountWith(TWO_STORED);
		testButton(0).click();
		testButton(1).click();
		await tick();
		expect(testButton(0).disabled).toBe(true);
		expect(testButton(1).disabled).toBe(true);

		emit(verdict("spare", true, "Key accepted by http://127.0.0.1:8000/v1."));
		await tick();
		expect(text(result(1))).toBe("Connected Key accepted by http://127.0.0.1:8000/v1.");
		expect(text(result(0))).toBe("");
		expect(text(testButton(0))).toBe("Testing…");

		emit(verdict("tower", false, "Authentication failed."));
		await tick();
		expect(text(result(0))).toBe("Not connected Authentication failed.");
		expect(text(result(1))).toBe("Connected Key accepted by http://127.0.0.1:8000/v1.");
	});

	it("says TST Desk did not answer after 30 seconds and turns Test back on", async () => {
		vi.useFakeTimers();
		await mountWith(setupState());
		testButton(0).click();
		await tick();
		vi.advanceTimersByTime(KEY_TEST_TIMEOUT_MS);
		await tick();
		expect(text(result(0))).toBe(`No answer ${KEY_TEST_COPY.noAnswer}`);
		expect(text(testButton(0))).toBe("Test");
		expect(testButton(0).disabled).toBe(false);
	});

	it("drops the result when the key's host changes", async () => {
		await mountWith(TWO_STORED);
		testButton(1).click();
		await tick();
		emit(verdict("spare", true, "Key accepted by http://127.0.0.1:8000/v1."));
		await tick();
		expect(text(result(1))).toContain("Connected");

		emit(
			setupState({
				credentials: [
					{ id: "tower", name: "TOWER", stored: true },
					{ id: "spare", name: "Spare", stored: true, base_url: "http://127.0.0.1:9000/v1" },
				],
			}),
		);
		await tick();
		expect(text(result(1))).toBe("");
	});

	it("shows a first-run verdict on no row, and no verdict below the Add form", async () => {
		await mountWith(TWO_STORED);
		testButton(0).click();
		await tick();
		emit(verdict(null, false, "Failed to connect."));
		await tick();
		expect(document.body.textContent).not.toContain("Failed to connect.");
		expect(text(testButton(0))).toBe("Testing…");

		emit(verdict("tower", true, "Key accepted."));
		await tick();
		const footer = document.body.querySelector(".add");
		expect(footer?.textContent).not.toContain("Key accepted.");
		expect(footer?.querySelector('[role="status"]')).toBeNull();
	});
});
