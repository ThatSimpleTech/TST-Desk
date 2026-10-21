// @vitest-environment jsdom
//
// The composer keeps the draft full width. Plan and the model dropdowns
// sit on a bar under the text, with Send on the right. The title bar does
// not repeat the model name.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";

import { DEFAULT_ATTACHMENT_LIMITS } from "./attachments";
import Composer from "./components/chat/Composer.svelte";
import TitleBar from "./components/TitleBar.svelte";
import { resetSession, session } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";
import { resetWorkspaces } from "./workspaces.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function mountComposer(value: string): void {
	app = mount(Composer, {
		target: document.body,
		props: {
			limits: { ...DEFAULT_ATTACHMENT_LIMITS },
			value,
			onsubmit: () => {},
		},
	});
}

beforeEach(() => {
	resetSession();
	resetSettings();
	session.sessionId = "s1";
	session.tier = "brain";
	session.modelSlugs = { brain: "kimi", worker: "flash", validator: "pro" };
	session.hosts = { brain: "192.0.2.49:4000" };
	session.preset = "vllm";
	settings.presets = ["vllm", "local"];
});

afterEach(async () => {
	if (app !== null) {
		await unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	resetSession();
	resetSettings();
	resetWorkspaces();
});

describe("composer model controls", () => {
	it("keeps the draft full width and the model bar under it", async () => {
		mountComposer("hello");
		await tick();

		const draft = document.querySelector(".draft");
		const bar = document.querySelector(".bar");
		const send = document.querySelector('[aria-label="Send message"]');
		const tier = document.querySelector<HTMLSelectElement>('[aria-label="Model tier"]');
		const field = document.querySelector("textarea");
		expect(draft?.contains(field ?? null)).toBe(true);
		expect(draft?.contains(tier ?? null)).toBe(false);
		expect(bar?.contains(tier ?? null)).toBe(true);
		expect(document.querySelector(".actions")?.contains(send ?? null)).toBe(true);
		expect(document.querySelector(".model-slot")?.contains(tier ?? null)).toBe(true);
		expect(tier!.compareDocumentPosition(send!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
		expect(tier?.value).toBe("brain");
		expect([...tier!.options].map((option) => option.text)).toEqual([
			"brain (kimi)",
			"worker (flash)",
			"validator (pro)",
		]);
		expect(document.querySelector('[aria-label="Session preset"]')).not.toBeNull();
		expect(document.body.textContent).toContain("Plan");
	});

	it("uses that same bar when the draft is more than one line", async () => {
		mountComposer("hello\nthere");
		await tick();

		const tier = document.querySelector('[aria-label="Model tier"]');
		expect(document.querySelector(".draft")?.contains(tier ?? null)).toBe(false);
		expect(document.querySelector(".model-slot")?.contains(tier ?? null)).toBe(true);
		expect(document.querySelector(".actions")?.contains(document.querySelector('[aria-label="Send message"]'))).toBe(
			true,
		);
	});

	it("lists each tier with its model in parentheses", async () => {
		session.modelSlugs = { brain: "moonshotai/kimi-k3", validator: "pro" };
		mountComposer("");
		await tick();

		const tier = document.querySelector<HTMLSelectElement>('[aria-label="Model tier"]');
		expect([...tier!.options].map((option) => option.text)).toEqual([
			"brain (moonshotai/kimi-k3)",
			"worker",
			"validator (pro)",
		]);
	});

	it("keeps worker and validator unavailable while Plan is on", async () => {
		session.planMode = true;
		mountComposer("");
		await tick();

		const tier = document.querySelector<HTMLSelectElement>('[aria-label="Model tier"]');
		expect(tier?.options[0]?.disabled).toBe(false);
		expect(tier?.options[1]?.disabled).toBe(true);
		expect(tier?.options[2]?.disabled).toBe(true);
	});

	it("leaves the model name out of the title bar", async () => {
		app = mount(TitleBar, {
			target: document.body,
			props: { pickDirectory: async () => null },
		});
		await tick();

		expect(document.querySelector("select")).toBeNull();
		expect(document.querySelector('[aria-label="Model"]')).toBeNull();
		expect(document.querySelector('[aria-label="Session preset"]')).toBeNull();
		const labels = [...document.querySelectorAll("button")].map((el) => el.textContent?.trim());
		expect(labels).not.toContain("Plan");
		expect(labels.some((label) => label?.includes("brain"))).toBe(false);
		expect(document.querySelector('[aria-label="Live model"]')).toBeNull();
		expect(document.body.textContent).not.toContain("192.0.2.49:4000");
	});
});
