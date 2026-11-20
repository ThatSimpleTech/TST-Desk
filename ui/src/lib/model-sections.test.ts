// @vitest-environment jsdom
//
// TD-5236: the picker's main list in sections, and the quick-switch slots.
// The daemon says which section each model is in and which slot it has; a
// slot's shortcut does what a click on that model's row does.

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
import { pickSlot } from "./model-pick";
import { modelAtSlot, sectionModels } from "./model-sections";
import { bindClient, resetSession, session } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";
import { modelSlot, resolveShortcut, sessionSlot } from "./shortcuts";

const ORDER = ["Local", "API", "Grok"];
let app: ReturnType<typeof mount> | null = null;
let sent: ClientMessageUnion[] = [];

beforeEach(() => {
	resetSession();
	resetSettings();
	sent = [];
	mocks.newChats = [];
	bindClient({
		send: (msg: ClientMessageUnion) => (sent.push(msg), true),
	} as unknown as ProtocolClient);
	session.sessionId = "s1";
	session.preset = "local";
	session.engine = "native";
	settings.hasModel = true;
	settings.activePreset = "local";
	settings.presets = ["api", "grok", "lab", "local"];
	settings.presetLabels = { api: "Hosted", grok: "Grok Build", lab: "Lab box", local: "Laptop" };
	settings.presetEngines = { grok: "grok" };
	settings.presetGroups = { api: "API", grok: "Grok", lab: "Trying out", local: "Local" };
	settings.pickerGroups = ORDER;
	settings.presetSlots = { local: 1, api: 2, grok: 3 };
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

describe("sectionModels", () => {
	it("puts the built-in sections first, in the daemon's order, then the rest by name", () => {
		const groups = { a: "Grok", b: "Zeta", c: "API", d: "Local", e: "Alpha", f: "API" };
		expect(sectionModels(["a", "b", "c", "d", "e", "f"], groups, ORDER)).toEqual([
			{ title: "Local", names: ["d"] },
			{ title: "API", names: ["c", "f"] },
			{ title: "Grok", names: ["a"] },
			{ title: "Alpha", names: ["e"] },
			{ title: "Zeta", names: ["b"] },
		]);
	});

	it("gives a single section no heading", () => {
		expect(sectionModels(["a", "b"], { a: "API", b: "API" }, ORDER)).toEqual([
			{ title: "", names: ["a", "b"] },
		]);
	});

	it("lists everything once when the daemon sent no sections", () => {
		expect(sectionModels(["a", "b"], {}, [])).toEqual([{ title: "", names: ["a", "b"] }]);
	});

	it("keeps a model the daemon gave no section, ahead of the titled ones", () => {
		expect(sectionModels(["a", "b"], { b: "API" }, ORDER)).toEqual([
			{ title: "", names: ["a"] },
			{ title: "API", names: ["b"] },
		]);
	});
});

describe("the model slot chord", () => {
	const press = (over: Partial<Parameters<typeof modelSlot>[0]>) => ({
		key: "1",
		code: "Digit1",
		metaKey: true,
		ctrlKey: false,
		altKey: true,
		shiftKey: false,
		...over,
	});
	const idle = { workspaceMenuOpen: false, paletteOpen: false, modalOpen: false, turnLive: false };

	it("is ⌘⌥ or Ctrl+Alt and a digit, read off the physical key", () => {
		expect(modelSlot(press({}))).toBe(1);
		expect(modelSlot(press({ metaKey: false, ctrlKey: true, code: "Digit9", key: "9" }))).toBe(9);
		// On a Mac, Option+1 types "¡": the key no longer says which digit.
		expect(modelSlot(press({ key: "¡" }))).toBe(1);
		expect(modelSlot(press({ code: undefined, key: "4" }))).toBe(4);
	});

	it("is nothing without Option, with Shift, or off the digit row", () => {
		expect(modelSlot(press({ altKey: false }))).toBeNull();
		expect(modelSlot(press({ shiftKey: true }))).toBeNull();
		expect(modelSlot(press({ metaKey: false }))).toBeNull();
		expect(modelSlot(press({ code: "Digit0", key: "0" }))).toBeNull();
		expect(modelSlot(press({ code: "KeyA", key: "a" }))).toBeNull();
	});

	it("does not take the session slots, and they do not take it", () => {
		expect(resolveShortcut(press({}), idle)).toBe("pick-model");
		expect(sessionSlot(press({}))).toBeNull();
		expect(resolveShortcut(press({ altKey: false }), idle)).toBe("pick-session");
	});

	it("does nothing over a modal", () => {
		expect(resolveShortcut(press({}), { ...idle, modalOpen: true })).toBeNull();
	});
});

describe("a slot's shortcut", () => {
	it("names the model in that slot", () => {
		expect(modelAtSlot(2, settings.presetSlots)).toBe("api");
		expect(modelAtSlot(7, settings.presetSlots)).toBeNull();
	});

	it("does what a click on that model does", () => {
		expect(pickSlot(2)).toBe(true);
		expect(sent).toEqual([
			{ type: "switch_preset", name: "api", session_id: "s1", make_default: true },
		]);
	});

	it("opens a new chat for a model on the other engine", () => {
		expect(pickSlot(3)).toBe(true);
		expect(sent).toEqual([{ type: "switch_preset", name: "grok", make_default: true }]);
		expect(mocks.newChats).toEqual(["grok"]);
	});

	it("does nothing for the model already in use, or an empty slot", () => {
		expect(pickSlot(1)).toBe(true);
		expect(pickSlot(7)).toBe(false);
		expect(sent).toEqual([]);
	});
});

describe("the picker", () => {
	async function openPicker(): Promise<HTMLElement> {
		app = mount(ModelPicker, { target: document.body });
		await tick();
		document.querySelector<HTMLButtonElement>('[aria-label="Model and effort"]')!.click();
		flushSync();
		return document.querySelector<HTMLElement>('[role="menu"]')!;
	}

	it("lists the models under Local, API, Grok, then a model's own section", async () => {
		const menu = await openPicker();
		// A label's own text, without the engine mark that sits inside it.
		const own = (el: Element): string => (el.firstChild?.textContent ?? "").trim();
		const seen = [...menu.querySelectorAll(".section, [role='menuitemradio'] .label")].map(
			(el) => (el.classList.contains("section") ? `# ${el.textContent}` : own(el)),
		);
		expect(seen).toEqual([
			"# Local",
			"Laptop",
			"# API",
			"Hosted",
			"# Grok",
			"Grok Build",
			"# Trying out",
			"Lab box",
		]);
	});

	it("shows each model's slot beside it", async () => {
		const menu = await openPicker();
		const slots = [...menu.querySelectorAll<HTMLElement>("kbd.slot")].map((el) => [
			el.textContent,
			el.title,
		]);
		expect(slots).toEqual([
			["1", "Quick switch: ⌘⌥1 (Ctrl+Alt+1)"],
			["2", "Quick switch: ⌘⌥2 (Ctrl+Alt+2)"],
			["3", "Quick switch: ⌘⌥3 (Ctrl+Alt+3)"],
		]);
	});

	it("shows no headings when every model is in one section", async () => {
		settings.presetGroups = { api: "API", grok: "API", lab: "API", local: "API" };
		const menu = await openPicker();
		expect(menu.querySelectorAll(".section")).toHaveLength(0);
		expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(4);
	});
});
