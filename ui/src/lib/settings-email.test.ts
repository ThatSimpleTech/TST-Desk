// @vitest-environment jsdom
//
// TD-3820: Settings → Email saves host/port/security/username/from and a
// password, then a test uses the saved config. The daemon's result is shown
// as the SMTP class and message.

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

function field(label: string): HTMLInputElement | HTMLSelectElement {
	const found = [...document.body.querySelectorAll("label")].find(
		(el) => el.querySelector("span")?.textContent === label,
	);
	const input = found?.querySelector("input, select");
	if (!(input instanceof HTMLInputElement) && !(input instanceof HTMLSelectElement)) {
		throw new Error(`missing field ${label}`);
	}
	return input;
}

function typeInto(input: HTMLInputElement | HTMLSelectElement, value: string): void {
	input.value = value;
	input.dispatchEvent(new Event("input", { bubbles: true }));
}

function button(name: string): HTMLButtonElement {
	const found = [...document.body.querySelectorAll("button")].find(
		(el) => el.textContent?.trim() === name,
	);
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

describe("Settings email", () => {
	it("saves the form and reports a test failure", async () => {
		openSettings("email");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		expect(button("Email").getAttribute("aria-current")).toBe("page");

		emit(
			setup({
				email: {
					enabled: false,
					host: "",
					port: 587,
					security: "starttls",
					username: "",
					from_address: "",
					password_stored: false,
				},
			}),
		);
		await tick();

		button("Off").click();
		await tick();
		typeInto(field("Host") as HTMLInputElement, "smtp.example.com");
		typeInto(field("Port") as HTMLInputElement, "587");
		typeInto(field("Username") as HTMLInputElement, "desk");
		typeInto(field("From") as HTMLInputElement, "desk@example.com");
		typeInto(field("Password") as HTMLInputElement, "s3cret-smtp-pw");
		button("Save").click();
		await tick();

		expect(mocks.sent.at(-1)).toEqual({
			type: "set_email_notify",
			enabled: true,
			host: "smtp.example.com",
			port: 587,
			security: "starttls",
			username: "desk",
			from_address: "desk@example.com",
			password: "s3cret-smtp-pw",
		});
		expect((field("Password") as HTMLInputElement).value).toBe("");

		emit(
			setup({
				email: {
					enabled: true,
					host: "smtp.example.com",
					port: 587,
					security: "starttls",
					username: "desk",
					from_address: "desk@example.com",
					password_stored: true,
				},
			}),
		);
		await tick();
		expect((field("Password") as HTMLInputElement).value).toBe("");
		expect((field("Host") as HTMLInputElement).value).toBe("smtp.example.com");

		typeInto(field("Test to") as HTMLInputElement, "owner@example.com");
		button("Send test email").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "test_email", to: "owner@example.com" });

		emit({
			type: "email_test_result",
			seq: 1,
			ok: false,
			error_class: "SMTPException",
			message: "auth failed",
		});
		await tick();
		expect(document.body.textContent).toContain("SMTPException: auth failed");
		expect(document.body.textContent).not.toContain("s3cret-smtp-pw");
	});
});
