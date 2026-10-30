// @vitest-environment jsdom
//
// The model picker: a short model list, an Effort submenu, the Fast and
// Ultracode switches, More models, Teams when one exists, and a chip that
// reads "<Model> <Effort>".

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
		setFast: (sessionId: string, on: boolean) => sent.push({ type: "set_fast", session_id: sessionId, on }),
		setUltracode: (sessionId: string, on: boolean) =>
			sent.push({ type: "set_ultracode", session_id: sessionId, on }),
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

	it("shows the effort submenu with the hint and badges, Low to Max", async () => {
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
		]);
		expect(document.querySelector(".item--accent")).toBeNull();
	});

	it("sends set_effort with the wire value and updates the chip", async () => {
		await click(chip());
		await click(item("Effort"));
		await click(item("Extra"));
		expect(sent).toEqual([{ type: "set_effort", session_id: "s1", effort: "xhigh" }]);
		expect(chip().textContent?.trim()).toBe("House Extra");

		await click(chip());
		await click(item("Effort"));
		await click(item("Max"));
		expect(sent.at(-1)).toEqual({ type: "set_effort", session_id: "s1", effort: "max" });
		expect(chip().textContent?.trim()).toBe("House Max");
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

	describe("fast (TD-5176)", () => {
		function tierState(extra: Record<string, unknown>, seq = 1): void {
			ingestEvent({
				type: "tier_state",
				seq,
				session_id: "s1",
				tier: "brain",
				override: null,
				model_slugs: {},
				preset: "house",
				...extra,
			} as never);
		}

		it("shows no Fast row for a model without one", async () => {
			await click(chip());
			expect(texts(".item").some((t) => t.startsWith("Fast"))).toBe(false);
		});

		it("offers Fast under Effort when the model has it, and sends set_fast", async () => {
			tierState({ fast_available: true });
			await tick();
			await click(chip());
			const labels = texts(".item");
			expect(labels.indexOf("Fast Off")).toBe(labels.indexOf("Effort Medium ›") + 1);
			expect(item("Fast").getAttribute("aria-checked")).toBe("false");
			await click(item("Fast"));
			expect(sent.at(-1)).toEqual({ type: "set_fast", session_id: "s1", on: true });
			expect(session.fastNote).toBe("Turning fast on…");
		});

		it("follows tier_state: on, the switch's note, and the chip tag", async () => {
			tierState({ fast_available: true, fast: true, fast_note: "Borrows all four GPUs; about 2 min" });
			await tick();
			expect(chip().textContent?.replace(/\s+/g, " ").trim()).toBe("House Medium · Fast");
			await click(chip());
			expect(item("Fast").getAttribute("aria-checked")).toBe("true");
			expect(item("Fast").textContent).toContain("Borrows all four GPUs; about 2 min");
			await click(item("Fast"));
			expect(sent.at(-1)).toEqual({ type: "set_fast", session_id: "s1", on: false });
		});

		it("drops the tag when the switch ended Fast on its own", async () => {
			tierState({ fast_available: true, fast: true });
			tierState({ fast_available: true, fast: false, fast_note: "Fast ended (idle 20 min)" }, 2);
			await tick();
			expect(chip().textContent?.trim()).toBe("House Medium");
			expect(session.fastNote).toBe("Fast ended (idle 20 min)");
		});
	});

	describe("ultracode (TD-5195)", () => {
		function tierState(extra: Record<string, unknown>, seq = 1): void {
			ingestEvent({
				type: "tier_state",
				seq,
				session_id: "s1",
				tier: "brain",
				override: null,
				model_slugs: {},
				preset: "house",
				...extra,
			} as never);
		}

		function chipText(): string {
			return chip().textContent?.replace(/\s+/g, " ").trim() ?? "";
		}

		it("hides the row on an older daemon that does not offer the switch", async () => {
			tierState({});
			await tick();
			await click(chip());
			expect(texts(".item").some((t) => t.startsWith("Ultracode"))).toBe(false);
		});

		it("puts the row right after Effort when Fast is not offered, in the accent style", async () => {
			tierState({ ultracode_available: true });
			await tick();
			await click(chip());
			const labels = texts(".item");
			const row = labels.indexOf("Ultracode Splits big jobs across workers Off");
			expect(row).toBe(labels.indexOf("Effort Medium ›") + 1);
			expect(item("Ultracode").getAttribute("role")).toBe("menuitemcheckbox");
			expect(item("Ultracode").getAttribute("aria-checked")).toBe("false");
			expect(item("Ultracode").classList.contains("item--accent")).toBe(true);
		});

		it("puts the row right after Fast when both are offered", async () => {
			tierState({ ultracode_available: true, fast_available: true });
			await tick();
			await click(chip());
			const labels = texts(".item");
			expect(labels.indexOf("Fast Off")).toBe(labels.indexOf("Effort Medium ›") + 1);
			expect(labels.findIndex((t) => t.startsWith("Ultracode"))).toBe(labels.indexOf("Fast Off") + 1);
		});

		it("sends set_ultracode, flips at once and keeps the menu open", async () => {
			tierState({ ultracode_available: true });
			await tick();
			await click(chip());
			await click(item("Ultracode"));
			expect(sent).toEqual([{ type: "set_ultracode", session_id: "s1", on: true }]);
			expect(session.ultracode).toBe(true);
			expect(document.querySelector(".menu")).not.toBeNull();
			expect(item("Ultracode").getAttribute("aria-checked")).toBe("true");
			expect(item("Ultracode").textContent).toContain("On");
			expect(chipText()).toBe("House Medium · Ultracode");
			await click(item("Ultracode"));
			expect(sent.at(-1)).toEqual({ type: "set_ultracode", session_id: "s1", on: false });
			expect(chipText()).toBe("House Medium");
		});

		it("survives an effort change, and the chip tags Fast then Ultracode", async () => {
			tierState({ ultracode_available: true, ultracode: true, fast_available: true, fast: true });
			await tick();
			expect(chipText()).toBe("House Medium · Fast · Ultracode");
			expect(document.querySelector(".ultracode-tag")?.textContent).toBe(" · Ultracode");
			await click(chip());
			await click(item("Effort"));
			await click(item("High"));
			expect(sent).toEqual([{ type: "set_effort", session_id: "s1", effort: "high" }]);
			expect(session.ultracode).toBe(true);
			expect(chipText()).toBe("House High · Fast · Ultracode");
		});

		it("follows tier_state off, and reads an older peer's effort ultracode as Max with the switch on", async () => {
			tierState({ ultracode_available: true, ultracode: true });
			tierState({ ultracode_available: true, ultracode: false }, 2);
			await tick();
			expect(chipText()).toBe("House Medium");
			tierState({ effort: "ultracode" }, 3);
			await tick();
			expect(session.effort).toBe("max");
			expect(session.ultracode).toBe(true);
			expect(session.ultracodeAvailable).toBe(false);
			expect(chipText()).toBe("House Max · Ultracode");
		});
	});
});
