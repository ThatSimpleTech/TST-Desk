// @vitest-environment jsdom
//
// TD-4817: mount-level coverage for the Settings → Model preset picker.
// The store tests prove the wiring; these prove what actually renders —
// per-tier models at a glance, the discovered fallback, key hints — and
// that a click or arrow key drives the same set_preset path with the
// in-flight guard intact. (The review found render-level ACs had zero
// coverage: the picker lookup and the draft reset could both break with
// every store test still green.)
//
// Seam is the same one the store tests mock: connection-status. The pane
// and the store are otherwise real.

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
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import SettingsPane from "./components/SettingsPane.svelte";
import { settings, startSettings, resetSettings, openSettings } from "./settings.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function setupState(over: Partial<SetupState> = {}): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["budget", "local", "tst-default"],
		active_preset: "local",
		tier_slugs: { brain: null, worker: null, validator: null },
		preset_models: {
			budget: {
				slugs: { brain: "cheap/b", worker: "cheap/w", validator: "cheap/v" },
				key_required: true,
			},
			local: { slugs: { brain: null, worker: null, validator: null }, key_required: false },
			"tst-default": {
				slugs: { brain: "pro/b", worker: "pro/w", validator: "pro/v" },
				key_required: true,
			},
		},
		...over,
	};
}

let app: ReturnType<typeof mount> | null = null;

async function renderPicker(): Promise<HTMLElement> {
	startSettings();
	emit(setupState());
	openSettings("model");
	app = mount(SettingsPane, { target: document.body });
	await tick();
	const el = document.body.querySelector('[role="radiogroup"][aria-label="Model preset"]');
	if (!(el instanceof HTMLElement)) throw new Error("preset picker did not mount");
	return el;
}

function radios(root: HTMLElement): NodeListOf<Element> {
	return root.querySelectorAll('[role="radio"]');
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
});

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	resetSettings();
});

describe("Settings preset picker (TD-4817)", () => {
	it("renders every preset with its tiers and key hint", async () => {
		const root = await renderPicker();
		expect(radios(root)).toHaveLength(3);

		const local = radios(root)[1];
		expect(local.textContent).toContain("no key needed");
		// A null slug renders as the discovery fallback, never an empty cell.
		expect(local.textContent).toContain("discovered from the endpoint");

		const def = radios(root)[2];
		expect(def.textContent).toContain("pro/b");
		expect(def.textContent).toContain("needs key");
	});

	it("clicks through to set_preset, refuses overlaps, and follows the ack", async () => {
		const root = await renderPicker();
		const budget = radios(root)[0];

		budget.dispatchEvent(new MouseEvent("click", { bubbles: true }));
		await tick();
		expect(mocks.sent).toEqual([{ type: "set_preset", name: "budget" }]);

		// In flight: a second click is refused by the store's guard.
		radios(root)[2].dispatchEvent(new MouseEvent("click", { bubbles: true }));
		await tick();
		expect(mocks.sent).toHaveLength(1);

		emit(setupState({ active_preset: "budget" }));
		await tick();
		expect(settings.activePreset).toBe("budget");
		expect(
			root.querySelector('[role="radio"][aria-checked="true"]')?.textContent,
		).toContain("budget");
		// The hint (outside the radiogroup) explains the session contract.
		expect(document.body.textContent).toContain("runs new sessions");
	});

	it("moves the choice with the arrow keys, focus following", async () => {
		const root = await renderPicker();
		// Active is "local" (index 1); ArrowDown moves to tst-default.
		root.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
		await tick();
		expect(mocks.sent).toEqual([{ type: "set_preset", name: "tst-default" }]);
		expect(document.activeElement).toBe(radios(root)[2]);

		// In flight: further arrows are ignored until the ack.
		root.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
		await tick();
		expect(mocks.sent).toHaveLength(1);

		// ArrowUp from local wraps to the first preset.
		emit(setupState());
		await tick();
		mocks.sent = [];
		root.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
		await tick();
		expect(mocks.sent).toEqual([{ type: "set_preset", name: "budget" }]);
		expect(document.activeElement).toBe(radios(root)[0]);
	});

	it("drops a half-typed slug draft when the preset switches", async () => {
		await renderPicker();
		const input = document.body.querySelector(".field input");
		if (!(input instanceof HTMLInputElement)) throw new Error("slug input not rendered");

		input.value = "half-typed";
		input.dispatchEvent(new Event("input", { bubbles: true }));
		await tick();

		// The ack flips the preset; the stale draft must not survive into it.
		emit(
			setupState({
				active_preset: "budget",
				tier_slugs: { brain: "cheap/b", worker: "cheap/w", validator: "cheap/v" },
			}),
		);
		await tick();
		expect(input.value).toBe("cheap/b");
	});
});
