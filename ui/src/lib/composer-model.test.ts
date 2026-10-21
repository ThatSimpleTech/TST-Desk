// @vitest-environment jsdom
//
// The composer keeps the draft full width. Plan and the model chip sit on
// a bar under the text, with Send on the right. There is no tier chip
// (TD-5172). The title bar does not repeat the model name.

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
	it("keeps the draft full width and Plan plus the model chip under it", async () => {
		mountComposer("hello");
		await tick();

		const draft = document.querySelector(".draft");
		const bar = document.querySelector(".bar");
		const send = document.querySelector('[aria-label="Send message"]');
		const picker = document.querySelector('[aria-label="Model and effort"]');
		const field = document.querySelector("textarea");
		expect(draft?.contains(field ?? null)).toBe(true);
		expect(draft?.contains(picker ?? null)).toBe(false);
		expect(bar?.contains(picker ?? null)).toBe(true);
		expect(document.querySelector(".actions")?.contains(send ?? null)).toBe(true);
		expect(document.querySelector(".model-slot")?.contains(picker ?? null)).toBe(true);
		expect(picker!.compareDocumentPosition(send!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
		expect(picker?.textContent).toContain("vllm Medium");
		expect(document.body.textContent).toContain("Plan");
	});

	it("has no brain/worker/validator chip (TD-5172)", async () => {
		mountComposer("hello\nthere");
		await tick();

		expect(document.querySelector('[aria-label="Model tier"]')).toBeNull();
		const options = [...document.querySelectorAll("option")].map((o) => o.textContent ?? "");
		expect(options.some((text) => /brain|worker|validator/.test(text))).toBe(false);
	});

	it("explains Plan in its tooltip", async () => {
		mountComposer("");
		await tick();

		const plan = [...document.querySelectorAll<HTMLButtonElement>("button")].find(
			(el) => el.textContent?.trim() === "Plan",
		);
		expect(plan?.title).toContain("shows you a plan");
		expect(plan?.title).toContain("approve");
	});

	it("shows the chip as Add a model before any model is set up", async () => {
		session.preset = "";
		settings.presets = [];
		settings.hasModel = false;
		mountComposer("");
		await tick();

		expect(document.querySelector('[aria-label="Model and effort"]')?.textContent?.trim()).toBe(
			"Add a model",
		);
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
