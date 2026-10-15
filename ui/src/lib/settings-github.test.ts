// @vitest-environment jsdom
//
// TD-4852: Settings → GitHub. Off hides Connect. A pending code can be
// copied and opened, and the browser is not opened for a non-https URL.

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
const openSpy = vi.fn();
const writeText = vi.fn();

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
	openSpy.mockReset();
	writeText.mockReset();
	writeText.mockResolvedValue(undefined);
	vi.stubGlobal("open", openSpy);
	Object.defineProperty(navigator, "clipboard", {
		configurable: true,
		value: { writeText },
	});
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
	vi.unstubAllGlobals();
});

describe("Settings GitHub", () => {
	it("explains that the connector is off and does not offer Connect", async () => {
		openSettings("github");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(setup({ github: { enabled: false, connected: false, phase: "off", message: "hidden" } }));
		await tick();
		expect(document.body.textContent).toContain(
			"GitHub is off until config.yaml sets connectors.github.client_id",
		);
		expect(document.body.textContent).not.toContain("hidden");
		expect(button("Connect GitHub")).toBeUndefined();
	});

	it("sends github_connect from an enabled idle section", async () => {
		openSettings("appearance");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit(
			setup({
				github: { enabled: true, connected: false, phase: "idle", message: "" },
			}),
		);
		await tick();
		requireButton("GitHub").click();
		await tick();
		requireButton("Connect GitHub").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "github_connect" });
	});

	it("shows a pending code, copies it, and opens only https", async () => {
		openSettings("github");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit({
			type: "github_status",
			seq: 1,
			enabled: true,
			connected: false,
			phase: "pending",
			user_code: "ABCD-1234",
			verification_uri: "https://example.test/login/device",
			login: null,
			message: "",
		});
		await tick();
		expect(document.body.textContent).toContain("ABCD-1234");
		expect(openSpy).not.toHaveBeenCalled();
		requireButton("Copy code").click();
		await tick();
		expect(writeText).toHaveBeenCalledWith("ABCD-1234");
		requireButton("Open browser").click();
		expect(openSpy).toHaveBeenCalledWith("https://example.test/login/device", "_blank", "noopener");

		emit({
			type: "github_status",
			seq: 1,
			enabled: true,
			connected: false,
			phase: "pending",
			user_code: "ABCD-1234",
			verification_uri: "javascript:alert(1)",
			login: null,
			message: "",
		});
		await tick();
		openSpy.mockClear();
		requireButton("Open browser").click();
		expect(openSpy).not.toHaveBeenCalled();
		requireButton("Disconnect").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "github_disconnect" });
	});

	it("shows the login and disconnects", async () => {
		openSettings("github");
		app = mount(SettingsPane, { target: document.body });
		await tick();
		emit({
			type: "github_status",
			seq: 1,
			enabled: true,
			connected: true,
			phase: "connected",
			user_code: "",
			verification_uri: "",
			login: "octocat",
			message: "",
		});
		await tick();
		expect(document.body.textContent).toContain("Signed in as octocat");
		requireButton("Disconnect").click();
		await tick();
		expect(mocks.sent.at(-1)).toEqual({ type: "github_disconnect" });
	});
});
