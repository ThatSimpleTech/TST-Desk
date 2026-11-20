// @vitest-environment jsdom
//
// TD-5239: a model can name its engine, and a chat keeps the engine it
// started on. The picker marks a Grok model and holds a switch across
// engines; Settings shows no roles for a Grok model that has none.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";

import ModelPicker from "./components/chat/ModelPicker.svelte";
import { engineClash, engineMark, newChatEngine } from "./model-engine";
import { resetSession, session } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	resetSession();
	resetSettings();
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	resetSession();
	resetSettings();
});

describe("engineMark", () => {
	it("marks only a model that names the Grok engine", () => {
		const engines = { grok: "grok", api: "native" } as const;
		expect(engineMark("grok", engines)).toBe("Grok");
		expect(engineMark("api", engines)).toBe("");
		expect(engineMark("local", engines)).toBe("");
	});
});

describe("newChatEngine", () => {
	it("is the active model's engine, else the Engine setting", () => {
		expect(newChatEngine("grok", { grok: "grok" }, "native")).toBe("grok");
		expect(newChatEngine("local", { grok: "grok" }, "native")).toBe("native");
		expect(newChatEngine("local", {}, "grok")).toBe("grok");
		expect(newChatEngine(null, { grok: "grok" }, "native")).toBe("native");
	});
});

describe("engineClash", () => {
	const engines = { grok: "grok", api: "native" } as const;

	it("holds a model on another engine than the chat's", () => {
		expect(engineClash("grok", engines, "native")).toContain("start a new chat");
		expect(engineClash("api", engines, "grok")).toContain("start a new chat");
	});

	it("lets through the same engine, a model that names none, and a chat not yet known", () => {
		expect(engineClash("grok", engines, "grok")).toBeNull();
		expect(engineClash("api", engines, "native")).toBeNull();
		expect(engineClash("local", engines, "native")).toBeNull();
		expect(engineClash("local", engines, "grok")).toBeNull();
		expect(engineClash("grok", engines, null)).toBeNull();
	});
});

describe("the model picker", () => {
	async function openPicker(): Promise<HTMLElement> {
		session.sessionId = "s1";
		session.preset = "local";
		session.engine = "native";
		settings.hasModel = true;
		settings.presets = ["api", "grok", "local"];
		settings.presetLabels = { api: "API", grok: "Grok Build", local: "Local" };
		settings.presetEngines = { grok: "grok" };
		app = mount(ModelPicker, { target: document.body });
		await tick();
		document.querySelector<HTMLButtonElement>('[aria-label="Model and effort"]')!.click();
		flushSync();
		return document.querySelector<HTMLElement>('[role="menu"]')!;
	}

	function row(menu: HTMLElement, label: string): HTMLButtonElement {
		const found = [...menu.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find(
			(button) => button.querySelector(".label")?.textContent?.includes(label),
		);
		if (found === undefined) throw new Error(`no row for ${label}`);
		return found;
	}

	it("marks the Grok model and no other", async () => {
		const menu = await openPicker();
		expect(row(menu, "Grok Build").querySelector(".badge")?.textContent).toBe("Grok");
		expect(row(menu, "API").querySelector(".badge")).toBeNull();
		expect(row(menu, "Local").querySelector(".badge")).toBeNull();
	});

	it("says the Grok model opens a new chat for a chat on TST Desk's own loop", async () => {
		const menu = await openPicker();
		const grok = row(menu, "Grok Build");
		// TD-5235: no longer held. It cannot move this chat, so it opens one.
		expect(grok.disabled).toBe(false);
		expect(grok.title).toContain("Opens a new chat");
		expect(grok.querySelector(".sub")?.textContent).toBe("Opens a new chat");
		expect(row(menu, "API").title).toBe("");
	});
});
