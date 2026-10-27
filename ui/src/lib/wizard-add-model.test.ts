// @vitest-environment jsdom
//
// The "Add a model" step renders each part of the walkthrough (TD-5172):
// URL, key, the server's model list, label and description, then the test.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";

import WizardPane from "./components/WizardPane.svelte";
import { onboarding, openAddModel, resetOnboarding } from "./onboarding.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function heading(): string {
	return document.querySelector(".step-title")?.textContent?.trim() ?? "";
}

function buttons(): string[] {
	return [...document.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "");
}

beforeEach(() => {
	resetOnboarding();
	openAddModel();
	app = mount(WizardPane, { target: document.body });
});

afterEach(async () => {
	if (app !== null) await unmount(app);
	app = null;
	document.body.replaceChildren();
	resetOnboarding();
});

describe("Add a model step", () => {
	it("starts on the server URL", async () => {
		await tick();
		expect(heading()).toBe("Add a model");
		expect(document.querySelector('input[type="url"]')).not.toBeNull();
		expect(buttons()).toContain("Continue");
	});

	it("asks for the key in a password field", async () => {
		onboarding.model.baseUrl = "https://api.example.com/v1";
		onboarding.model.sub = "key";
		await tick();
		expect(document.querySelector('input[type="password"]')).not.toBeNull();
		expect(document.body.textContent).toContain("https://api.example.com/v1");
		expect(buttons()).toContain("List models");
	});

	it("lists the server's models with size and price", async () => {
		onboarding.model.models = [
			{ id: "vendor/model-a", context_window: 128000, input_price: 3, output_price: 15 },
			{ id: "vendor/model-b" },
		];
		onboarding.model.sub = "pick";
		await tick();
		const rows = [...document.querySelectorAll('[role="option"]')].map((el) =>
			el.textContent?.replace(/\s+/g, " ").trim(),
		);
		expect(rows[0]).toContain("vendor/model-a");
		expect(rows[0]).toContain("128k");
		expect(rows[0]).toContain("$3.00 / $15.00 per M");
		expect(rows[1]).toBe("vendor/model-b");
	});

	it("names it, then shows the test reply", async () => {
		onboarding.model.slug = "vendor/model-a";
		onboarding.model.label = "model-a";
		onboarding.model.sub = "label";
		await tick();
		const label = document.querySelector<HTMLInputElement>('input[maxlength="40"]');
		expect(label?.value).toBe("model-a");
		expect(buttons()).toContain("Save model");

		onboarding.model.sub = "test";
		onboarding.model.test = { ok: true, reply: "I am working.", detail: "" };
		await tick();
		expect(document.body.textContent).toContain("It replied: “I am working.”");
		expect(buttons()).toContain("Continue");
	});
});

describe("Find models on this computer (TD-5180)", () => {
	const ollama = {
		name: "Ollama",
		base_url: "http://127.0.0.1:11434/v1",
		models: [{ id: "llama3.2:3b" }, { id: "qwen3:8b" }],
	};

	it("offers the button on the URL step", async () => {
		await tick();
		expect(buttons()).toContain("Find models on this computer");
	});

	it("lists what answered, and a click opens its model list", async () => {
		onboarding.model.scan = { servers: [ollama], checked: [ollama.base_url] };
		await tick();
		const found = [...document.querySelectorAll<HTMLButtonElement>(".scan .preset")];
		expect(found.map((b) => b.textContent?.replace(/\s+/g, " ").trim())).toEqual([
			"Ollama 2 models · http://127.0.0.1:11434/v1",
		]);
		expect(buttons()).toContain("Look again");
		found[0].click();
		await tick();
		expect(onboarding.model.sub).toBe("pick");
		expect(document.body.textContent).toContain("2 models at");
		expect(buttons()).toContain("qwen3:8b");
	});

	it("says which ports it checked when nothing answered", async () => {
		onboarding.model.scan = {
			servers: [],
			checked: ["http://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1"],
		};
		await tick();
		expect(document.body.textContent).toContain("Nothing answered on this computer (ports 11434, 1234)");
	});

	it("says the list is empty when config names no servers", async () => {
		onboarding.model.scan = { servers: [], checked: [] };
		await tick();
		expect(document.body.textContent).toContain("local_servers in config.yaml is empty");
	});
});
