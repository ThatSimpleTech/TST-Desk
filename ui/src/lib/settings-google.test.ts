// @vitest-environment jsdom
//
// TD-4854: Settings → Email. An empty client id hides Google and shows
// how to turn it on. Sign-in, the client secret, and disconnect are
// daemon verbs. An expired grant shows Sign in and Disconnect.

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

function field(label: string): HTMLInputElement {
	const found = [...document.body.querySelectorAll("label")].find(
		(el) => el.querySelector("span")?.textContent === label,
	);
	const input = found?.querySelector("input");
	if (!(input instanceof HTMLInputElement)) throw new Error(`missing field ${label}`);
	return input;
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

describe("Settings Google email", () => {
	it("hides sign-in until a client id is configured", async () => {
		openSettings("email");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(
			setup({
				google: {
					enabled: false,
					connected: false,
					phase: "off",
					message: "hidden-secret",
				},
			}),
		);
		await tick();
		expect(document.body.textContent).toContain(
			"Google sign-in is off until config.yaml sets connectors.google.client_id",
		);
		expect(document.body.textContent).toContain("connectors.google.gmail_host");
		expect(document.body.textContent).not.toContain("hidden-secret");
		expect(document.body.textContent).not.toContain("accounts.google.com");
		expect(document.body.textContent).not.toContain("gmail.googleapis.com");
		expect(button("Google")).toBeUndefined();
		expect(button("Sign in with Google")).toBeUndefined();
		expect(button("SMTP")).toBeDefined();
	});

	it("saves a typed secret, signs in, and still sends a test", async () => {
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
				google: {
					enabled: true,
					connected: false,
					phase: "idle",
					message: "",
					client_secret_stored: false,
				},
			}),
		);
		await tick();
		requireButton("Google").click();
		await tick();
		const names = [...document.body.querySelectorAll(".field-name")].map(
			(el) => el.textContent,
		);
		expect(names).not.toContain("Password");
		expect(names).not.toContain("Host");
		expect(names).toContain("From");
		expect(names).toContain("Client secret");
		requireButton("Save").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({
			type: "set_email_notify",
			enabled: true,
			method: "google",
			host: "smtp.example.com",
			port: 587,
			security: "starttls",
			username: "desk",
			from_address: "desk@example.com",
			password: null,
		});

		const secret = field("Client secret");
		secret.value = "desk-client-secret";
		secret.dispatchEvent(new Event("input", { bubbles: true }));
		requireButton("Save").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({
			type: "set_email_notify",
			enabled: true,
			method: "google",
			host: "smtp.example.com",
			port: 587,
			security: "starttls",
			username: "desk",
			from_address: "desk@example.com",
			password: null,
			google_client_secret: "desk-client-secret",
		});
		expect(field("Client secret").value).toBe("");

		requireButton("Sign in with Google").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "google_connect" });

		emit({
			type: "google_status",
			seq: 1,
			enabled: true,
			connected: true,
			phase: "connected",
			account: "owner@example.com",
			message: "",
			client_secret_stored: true,
		});
		await tick();
		expect(document.body.textContent).toContain("Signed in as owner@example.com");
		expect(button("Sign in with Google")).toBeUndefined();
		requireButton("Disconnect").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "google_disconnect" });

		const test = document.body.querySelector('input[type="email"]');
		if (!(test instanceof HTMLInputElement)) throw new Error("missing test field");
		test.value = "owner@example.com";
		test.dispatchEvent(new Event("input", { bubbles: true }));
		requireButton("Send test email").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "test_email", to: "owner@example.com" });
	});

	it("shows sign-in and disconnect when the grant has expired", async () => {
		openSettings("email");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(
			setup({
				email: { enabled: true, method: "google", from_address: "desk@example.com" },
				google: {
					enabled: true,
					connected: true,
					phase: "expired",
					account: "owner@example.com",
					message:
						"Google sign-in expired. An unverified app, or a client still in testing, stops refreshing until you reconnect. Reconnect in Settings.",
					client_secret_stored: true,
				},
			}),
		);
		await tick();
		expect(document.body.textContent).toContain("reconnect");
		expect(document.body.textContent).toContain("testing");
		expect(document.body.textContent).not.toContain("Signed in as");
		expect(button("Sign in with Google")).toBeDefined();
		expect(button("Disconnect")).toBeDefined();
	});
});
