// @vitest-environment jsdom
//
// The model picker: a short model list, an Effort submenu, More models,
// Teams when one exists, and a chip that reads "<Model> <Effort>".

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";

import type { ProtocolClient } from "./client";
import ModelPicker from "./components/chat/ModelPicker.svelte";
import {
	bindClient,
	ingestEvent,
	resetSession,
	session,
} from "./session-status.svelte.js";
import { onboarding, resetOnboarding } from "./onboarding.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;
const sent: unknown[] = [];

function fakeClient(): ProtocolClient {
	return {
		setEffort: (sessionId: string, effort: string) =>
			sent.push({ type: "set_effort", session_id: sessionId, effort }),
		setSessionPreset: (sessionId: string, name: string) =>
			sent.push({ type: "set_session_preset", session_id: sessionId, name }),
	} as unknown as ProtocolClient;
}

function chip(): HTMLButtonElement {
	return document.querySelector<HTMLButtonElement>("button.chip")!;
}

function texts(selector: string): string[] {
	return [...document.querySelectorAll(selector)].map((el) => el.textContent?.replace(/\s+/g, " ").trim() ?? "");
}

async function click(el: Element | null | undefined): Promise<void> {
	(el as HTMLElement).click();
	await tick();
}

function item(label: string): HTMLElement {
	const found = [...document.querySelectorAll<HTMLElement>(".item")].find((el) =>
		el.textContent?.trim().startsWith(label),
	);
	if (!found) throw new Error(`no menu row starting ${label}`);
	return found;
}

beforeEach(() => {
	sent.length = 0;
	resetSession();
	resetSettings();
	bindClient(fakeClient());
	session.sessionId = "s1";
	session.preset = "house";
	settings.presets = ["house", "tst-default", "sub-claude-opus", "sub-grok", "house-raw"];
	settings.presetLabels = {
		house: "House",
		"tst-default": "Default",
		"sub-claude-opus": "Claude Opus",
		"sub-grok": "Grok",
	};
	settings.presetDescriptions = { house: "Coding and chat on your own server" };
	settings.morePresets = ["sub-claude-opus", "sub-grok", "house-raw"];
	app = mount(ModelPicker, { target: document.body });
});

afterEach(async () => {
	if (app !== null) await unmount(app);
	app = null;
	document.body.replaceChildren();
	bindClient(null);
	resetSession();
	resetSettings();
	resetOnboarding();
	vi.restoreAllMocks();
});

describe("model picker", () => {
	it("defaults to Medium and the chip reads House Medium", async () => {
		await tick();
		expect(session.effort).toBe("medium");
		expect(chip().textContent?.trim()).toBe("House Medium");
	});

	it("lists the labelled models with their descriptions, then Effort, More, Add and the guide", async () => {
		await click(chip());
		expect(texts(".item")).toEqual([
			"House Coding and chat on your own server ✓",
			"Default",
			"Effort Medium ›",
			"More models ›",
			"Add a model",
			"How models work",
		]);
		expect(document.querySelector(".sub")?.textContent).toBe("Coding and chat on your own server");
		expect(document.querySelector('[aria-checked="true"]')?.textContent).toContain("House");
	});

	it("puts the labelled raw routes under More models; unlabelled ones are hidden", async () => {
		await click(chip());
		await click(item("More models"));
		expect(texts(".item")).toEqual(["Claude Opus", "Grok"]);
		await click(item("Grok"));
		expect(sent).toEqual([{ type: "set_session_preset", session_id: "s1", name: "sub-grok" }]);
	});

	it("shows Teams only when a team exists", async () => {
		await click(chip());
		expect(texts(".item")).not.toContain("Teams ›");
		await click(chip());
		settings.presets = [...settings.presets, "crew"];
		settings.presetLabels = { ...settings.presetLabels, crew: "Crew" };
		settings.teamPresets = ["crew"];
		await tick();
		await click(chip());
		expect(texts(".item")).toContain("Teams ›");
		expect(texts(".item")).not.toContain("Crew");
		await click(item("Teams"));
		expect(texts(".item")).toEqual(["Crew"]);
	});

	it("Add a model opens the walkthrough", async () => {
		await click(chip());
		await click(item("Add a model"));
		expect(onboarding.open).toBe(true);
		expect(onboarding.step).toBe("model");
		expect(document.querySelector(".menu")).toBeNull();
	});

	it("with no model yet the chip reads Add a model and opens the walkthrough", async () => {
		settings.hasModel = false;
		await tick();
		expect(chip().textContent?.trim()).toBe("Add a model");
		await click(chip());
		expect(onboarding.open).toBe(true);
		expect(document.querySelector(".menu")).toBeNull();
	});

	it("shows the effort submenu with the hint, badges and Ultracode last", async () => {
		await click(chip());
		await click(item("Effort"));
		expect(document.querySelector(".hint")?.textContent).toBe(
			"Higher effort means more thorough responses, but takes longer.",
		);
		expect(texts(".item")).toEqual([
			"Low",
			"Medium Recommended ✓",
			"High",
			"Extra",
			"Max slower, uses the big model",
			"Ultracode",
		]);
		expect(document.querySelector(".item--accent")?.textContent).toContain("Ultracode");
	});

	it("sends set_effort with the wire value and updates the chip", async () => {
		await click(chip());
		await click(item("Effort"));
		await click(item("Extra"));
		expect(sent).toEqual([{ type: "set_effort", session_id: "s1", effort: "xhigh" }]);
		expect(chip().textContent?.trim()).toBe("House Extra");

		await click(chip());
		await click(item("Effort"));
		await click(item("Ultracode"));
		expect(sent.at(-1)).toEqual({ type: "set_effort", session_id: "s1", effort: "ultracode" });
		expect(chip().textContent?.trim()).toBe("House Ultracode");
	});

	it("follows the effort a tier_state reports, so it persists across a reattach", async () => {
		ingestEvent({
			type: "tier_state",
			seq: 1,
			session_id: "s1",
			tier: "brain",
			override: null,
			model_slugs: {},
			preset: "house",
			effort: "max",
		} as never);
		await tick();
		expect(chip().textContent?.trim()).toBe("House Max");

		ingestEvent({
			type: "tier_state",
			seq: 2,
			session_id: "s1",
			tier: "brain",
			override: null,
			model_slugs: {},
			preset: "house",
		} as never);
		await tick();
		expect(chip().textContent?.trim()).toBe("House Medium");
	});
});
