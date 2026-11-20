// @vitest-environment jsdom
//
// TD-5235: the picker's click. It moves this chat and makes the model the
// one new chats use; "Only this chat" moves just the chat; a model picked
// while a turn runs waits for the turn; a model on the other engine opens a
// new chat on it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ newChats: [] as (string | undefined)[] }));

vi.mock("./sessions.svelte.js", () => ({
	newSession: (preset?: string) => {
		mocks.newChats.push(preset);
		return true;
	},
}));

import ModelPicker from "./components/chat/ModelPicker.svelte";
import type { ProtocolClient } from "./client";
import { bindClient, ingestEvent, resetSession, session } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;
let sent: ClientMessageUnion[] = [];

function fakeClient(): ProtocolClient {
	return {
		send: (msg: ClientMessageUnion) => (sent.push(msg), true),
		setSessionPreset: (sessionId: string, name: string) =>
			sent.push({ type: "set_session_preset", session_id: sessionId, name }),
	} as unknown as ProtocolClient;
}

beforeEach(() => {
	resetSession();
	resetSettings();
	sent = [];
	mocks.newChats = [];
	bindClient(fakeClient());
	session.sessionId = "s1";
	session.preset = "local";
	session.engine = "native";
	settings.hasModel = true;
	settings.activePreset = "local";
	settings.presets = ["api", "grok", "local"];
	settings.presetLabels = { api: "API", grok: "Grok Build", local: "Local" };
	settings.presetEngines = { grok: "grok" };
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	bindClient(null);
	resetSession();
	resetSettings();
});

async function openPicker(): Promise<HTMLElement> {
	app = mount(ModelPicker, { target: document.body });
	await tick();
	chip().click();
	flushSync();
	return document.querySelector<HTMLElement>('[role="menu"]')!;
}

function chip(): HTMLButtonElement {
	return document.querySelector<HTMLButtonElement>('[aria-label="Model and effort"]')!;
}

function row(menu: HTMLElement, label: string): HTMLButtonElement {
	const found = [...menu.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find(
		(button) => button.querySelector(".label")?.textContent?.includes(label),
	);
	if (found === undefined) throw new Error(`no row for ${label}`);
	return found;
}

function only(menu: HTMLElement, label: string): HTMLButtonElement | null {
	return menu.querySelector<HTMLButtonElement>(`[aria-label="Only this chat: ${label}"]`);
}

describe("picking a model", () => {
	it("moves this chat and makes it the model new chats use", async () => {
		const menu = await openPicker();
		row(menu, "API").click();
		expect(sent).toEqual([
			{ type: "switch_preset", name: "api", session_id: "s1", make_default: true },
		]);
		expect(mocks.newChats).toEqual([]);
	});

	it("Only this chat leaves the model new chats use alone", async () => {
		const menu = await openPicker();
		only(menu, "API")!.click();
		expect(sent).toEqual([
			{ type: "switch_preset", name: "api", session_id: "s1", make_default: false },
		]);
	});

	it("offers Only this chat on every model but the current one and the other engine's", async () => {
		const menu = await openPicker();
		expect(only(menu, "API")).not.toBeNull();
		expect(only(menu, "Local")).toBeNull();
		expect(only(menu, "Grok Build")).toBeNull();
	});

	it("does nothing for the model the chat and new chats already use", async () => {
		const menu = await openPicker();
		row(menu, "Local").click();
		expect(sent).toEqual([]);
	});

	it("makes the chat's own model the default when new chats use another", async () => {
		settings.activePreset = "api";
		const menu = await openPicker();
		row(menu, "Local").click();
		expect(sent).toEqual([
			{ type: "switch_preset", name: "local", session_id: "s1", make_default: true },
		]);
	});
});

describe("picking a model on the other engine", () => {
	it("opens a new chat on it and makes it the model new chats use", async () => {
		const menu = await openPicker();
		row(menu, "Grok Build").click();
		expect(sent).toEqual([{ type: "switch_preset", name: "grok", make_default: true }]);
		expect(mocks.newChats).toEqual(["grok"]);
	});
});

describe("picking a model while a turn runs", () => {
	it("is allowed, and says it applies when the turn finishes", async () => {
		session.turnActive = true;
		const menu = await openPicker();
		const api = row(menu, "API");
		expect(api.disabled).toBe(false);
		expect(api.title).toBe("Applies when this turn finishes");
		api.click();
		expect(sent).toEqual([
			{ type: "switch_preset", name: "api", session_id: "s1", make_default: true },
		]);
	});

	it("shows on the chip what is waiting, and clears when it lands", async () => {
		app = mount(ModelPicker, { target: document.body });
		await tick();
		const state = {
			type: "tier_state",
			seq: 2,
			session_id: "s1",
			tier: "brain",
			model_slugs: {},
			preset: "local",
			pending_preset: "api",
		} as const;
		ingestEvent(state as never);
		flushSync();
		expect(session.pendingPreset).toBe("api");
		expect(chip().textContent).toContain("Local Medium");
		expect(chip().textContent).toContain("→ API after this turn");
		ingestEvent({ ...state, seq: 3, preset: "api", pending_preset: null } as never);
		flushSync();
		expect(session.pendingPreset).toBeNull();
		expect(chip().textContent).not.toContain("after this turn");
		expect(chip().textContent).toContain("API Medium");
	});
});

describe("a chat on another computer", () => {
	it("keeps the older verb, which its hub forwards, and its own rules", async () => {
		session.sessionId = "s1:mch_0123456789ab";
		session.turnActive = true;
		const menu = await openPicker();
		expect(row(menu, "API").disabled).toBe(true);
		expect(row(menu, "API").title).toBe("Cannot change model while a turn is running");
		expect(only(menu, "API")).toBeNull();
		session.turnActive = false;
		flushSync();
		row(menu, "API").click();
		expect(sent).toEqual([{ type: "set_session_preset", session_id: "s1:mch_0123456789ab", name: "api" }]);
	});
});
