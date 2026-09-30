// @vitest-environment jsdom
//
// TD-4838: API-key fields must not look like an sk- format requirement,
// and a failed Test has to show the daemon's reason.

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
import { onboarding, resetOnboarding } from "./onboarding.svelte.js";
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
			{ id: "ezer", name: "EZER", stored: true },
			{ id: "local", name: "Local", stored: false },
		],
		...over,
	};
}

function button(name: string, index: number): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].filter(
		(el) => el.textContent?.trim() === name,
	)[index];
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	resetSettings();
	resetOnboarding();
	startSettings();
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	resetSettings();
	resetOnboarding();
});

describe("API key fields (TD-4838)", () => {
	it("uses a neutral placeholder and enables Test when a key is stored", async () => {
		emit(setupState());
		app = mount(SettingsKeys, { target: document.body });
		await tick();

		const placeholders = [...document.body.querySelectorAll("input")].map((el) =>
			el.getAttribute("placeholder"),
		);
		expect(placeholders).toContain("Paste API key");
		expect(placeholders.some((value) => value?.startsWith("sk-"))).toBe(false);

		expect(button("Test", 0).disabled).toBe(false);
		expect(button("Test", 1).disabled).toBe(true);
		button("Test", 0).click();
		expect(mocks.sent.at(-1)).toEqual({ type: "validate_api_key", credential: "ezer" });
	});

	it("shows the daemon reason when Test fails", async () => {
		emit(setupState());
		app = mount(SettingsKeys, { target: document.body });
		await tick();

		onboarding.validation = {
			ok: false,
			detail: "Re-save the key in Settings → API keys",
		};
		await tick();
		const fail = document.querySelector(".test-fail");
		expect(fail?.textContent).toContain("Re-save the key in Settings → API keys");

		onboarding.validation = { ok: false, detail: "" };
		await tick();
		expect(document.querySelector(".test-fail")?.textContent).toContain("The key was rejected.");

		onboarding.validation = { ok: true, detail: "Key accepted by the active preset's provider." };
		await tick();
		expect(document.querySelector(".test-fail")).toBeNull();
		expect(document.body.textContent).toContain("Key accepted by the active preset's provider.");
	});
});
