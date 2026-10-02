// @vitest-environment jsdom
//
// TD-4853: Settings → Email. An empty client id hides Microsoft and shows
// how to turn it on. Sign-in and disconnect are daemon verbs. The test
// button stays available for both delivery methods.

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

import SettingsPane from "./components/SettingsPane.svelte";
import { openSettings, resetSettings, startSettings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function setup(over: Partial<SetupState> = {}): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["local"],
		active_preset: "local",
		...over,
	};
}

function button(name: string): HTMLButtonElement | undefined {
	return [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	) as HTMLButtonElement | undefined;
}

function requireButton(name: string): HTMLButtonElement {
	const found = button(name);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
	return found;
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
	document.body.replaceChildren();
	resetSettings();
});

describe("Settings Microsoft email", () => {
	it("hides sign-in until a client id is configured", async () => {
		openSettings("email");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(
			setup({
				microsoft: {
					enabled: false,
					connected: false,
					phase: "off",
					message: "hidden-secret",
				},
			}),
		);
		await tick();
		expect(document.body.textContent).toContain(
			"Microsoft sign-in is off until config.yaml sets connectors.microsoft.client_id",
		);
		expect(document.body.textContent).not.toContain("hidden-secret");
		expect(button("Microsoft")).toBeUndefined();
		expect(button("Sign in with Microsoft")).toBeUndefined();
		expect(button("SMTP")).toBeDefined();
	});

	it("signs in, shows the account, and still sends a test", async () => {
		openSettings("email");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(
			setup({
				email: {
					enabled: true,
					method: "smtp",
					host: "smtp.example.com",
					port: 587,
					security: "starttls",
					username: "desk",
					from_address: "desk@example.com",
					password_stored: true,
				},
				microsoft: { enabled: true, connected: false, phase: "idle", message: "" },
			}),
		);
		await tick();
		requireButton("Microsoft").click();
		await tick();
		const names = [...document.body.querySelectorAll(".field-name")].map(
			(el) => el.textContent,
		);
		expect(names).not.toContain("Password");
		expect(names).not.toContain("Host");
		expect(names).toContain("From");
		requireButton("Save").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({
			type: "set_email_notify",
			enabled: true,
			method: "microsoft",
			host: "smtp.example.com",
			port: 587,
			security: "starttls",
			username: "desk",
			from_address: "desk@example.com",
			password: null,
		});
		requireButton("Sign in with Microsoft").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "microsoft_connect" });

		emit({
			type: "microsoft_status",
			seq: 1,
			enabled: true,
			connected: true,
			phase: "connected",
			account: "owner@example.com",
			message: "",
		});
		await tick();
		expect(document.body.textContent).toContain("Signed in as owner@example.com");
		expect(button("Sign in with Microsoft")).toBeUndefined();
		requireButton("Disconnect").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "microsoft_disconnect" });

		const test = document.body.querySelector('input[type="email"]');
		if (!(test instanceof HTMLInputElement)) throw new Error("missing test field");
		test.value = "owner@example.com";
		test.dispatchEvent(new Event("input", { bubbles: true }));
		requireButton("Send test email").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "test_email", to: "owner@example.com" });
	});
});
