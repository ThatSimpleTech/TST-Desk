// @vitest-environment jsdom
//
// The tier picker lives in the composer: beside Send while the draft is one
// line, and in a footer under the text once the draft breaks onto a second
// line. The title bar keeps the live model readout.

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
	session.hosts = { brain: "192.168.1.49:4000" };
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
	it("places brain, worker, and validator to the right of Send on one line", async () => {
		mountComposer("hello");
		await tick();

		const entry = document.querySelector("[data-model-placement]");
		expect(entry?.getAttribute("data-model-placement")).toBe("inline");

		const send = document.querySelector('[aria-label="Send message"]');
		const tier = document.querySelector<HTMLSelectElement>('[aria-label="Model tier"]');
		expect(send).not.toBeNull();
		expect(tier?.value).toBe("brain");
		expect([...tier!.options].map((option) => option.text)).toEqual([
			"brain (kimi)",
			"worker (flash)",
			"validator (pro)",
		]);
		expect(send!.compareDocumentPosition(tier!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
		expect(document.querySelector('[aria-label="Session preset"]')).not.toBeNull();
		expect(document.body.textContent).toContain("Plan");
	});

	it("drops the controls into a footer once the draft has a second line", async () => {
		mountComposer("hello\nthere");
		await tick();

		expect(document.querySelector("[data-model-placement]")?.getAttribute("data-model-placement")).toBe(
			"footer",
		);
		const tier = document.querySelector('[aria-label="Model tier"]');
		expect(document.querySelector(".actions")?.contains(tier ?? null)).toBe(false);
		expect(document.querySelector(".model-slot")?.contains(tier ?? null)).toBe(true);
		const send = document.querySelector('[aria-label="Send message"]');
		expect(send!.compareDocumentPosition(tier!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
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

	it("leaves the tier picker out of the title bar and keeps the live model there", async () => {
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
		expect(document.querySelector('[aria-label="Live model"]')?.textContent).toBe(
			"kimi · 192.168.1.49:4000",
		);
	});
});
