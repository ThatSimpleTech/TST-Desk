// @vitest-environment jsdom
//
// TD-5241: Settings → Model edits the model you choose, not only the one
// new chats use. The chosen model's roles come from the daemon
// (`preset_roles`); every save names that model; and choosing one switches
// nothing.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, SetupState } from "./protocol";

const mocks = vi.hoisted(() => ({
	handlers: new Set<(e: DaemonEventUnion) => void>(),
	sent: [] as ClientMessageUnion[],
}));

// Every subscriber, not the latest: the page's own parts listen too.
vi.mock("./connection-status.svelte.js", () => {
	const onEvent = (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.add(handler);
		return () => mocks.handlers.delete(handler);
	};
	return {
		onEvent,
		onDaemonEvent: onEvent,
		sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
	};
});

import SettingsModels from "./components/SettingsModels.svelte";
import {
	chooseEditPreset,
	editTarget,
	isDiscovered,
	resetSettings,
	roleView,
	saveContextWindow,
	saveSlug,
	saveTierCredential,
	selectedCredential,
	settings,
	startSettings,
} from "./settings.svelte.js";

const CARD = {
	input_price: 0,
	output_price: 0,
	cache_read_price: 0,
	context_window: 32768,
	max_output_tokens: 4096,
};

function setupState(over: Partial<SetupState> = {}): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		has_model: true,
		presets: ["api", "grok", "local"],
		active_preset: "api",
		preset_labels: { api: "Hosted", grok: "Grok", local: "Laptop" },
		preset_engines: { grok: "grok" },
		tier_slugs: { brain: "vendor/big", worker: "vendor/small", validator: "vendor/small" },
		tier_credentials: { brain: "openrouter", worker: "openrouter", validator: "openrouter" },
		tier_loopback: { brain: false, worker: false, validator: false },
		tier_cards: { brain: { ...CARD, context_window: 200000 } },
		...over,
	} as SetupState;
}

const LOCAL_ROLES = {
	type: "preset_roles",
	seq: 1,
	preset: "local",
	tier_slugs: { brain: null, worker: null, validator: null },
	tier_credentials: { brain: null, worker: null, validator: null },
	tier_loopback: { brain: true, worker: true, validator: true },
	tier_cards: { brain: CARD, worker: CARD, validator: CARD },
} as unknown as DaemonEventUnion;

function emit(event: DaemonEventUnion): void {
	for (const handler of [...mocks.handlers]) handler(event);
	flushSync();
}

let stop: (() => void) | null = null;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	mocks.sent = [];
	resetSettings();
	stop = startSettings();
	emit(setupState());
	mocks.sent = [];
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	stop?.();
	resetSettings();
});

describe("choosing the model to edit", () => {
	it("edits the model new chats use until another is chosen", () => {
		expect(editTarget()).toBe("api");
		expect(roleView().slugs.brain).toBe("vendor/big");
		expect(mocks.sent).toEqual([]);
	});

	it("asks the daemon for the chosen model's roles and shows nothing until they come", () => {
		chooseEditPreset("local");
		expect(mocks.sent).toEqual([{ type: "get_preset_roles", name: "local" }]);
		expect(editTarget()).toBe("local");
		// Not the active model's roles under another model's name.
		expect(roleView()).toEqual({ slugs: {}, credentials: {}, loopback: {}, cards: {} });
		emit(LOCAL_ROLES);
		expect(roleView().loopback.brain).toBe(true);
		expect(isDiscovered("brain")).toBe(true);
		expect(selectedCredential("brain")).toBe("");
		// The active model's own roles are untouched.
		expect(settings.tierSlugs.brain).toBe("vendor/big");
		expect(settings.activePreset).toBe("api");
	});

	it("ignores roles for a model that is not the one being edited", () => {
		chooseEditPreset("local");
		emit({ ...LOCAL_ROLES, preset: "grok" } as DaemonEventUnion);
		expect(settings.editRoles).toBeNull();
	});

	it("choosing the active model again needs no ask", () => {
		chooseEditPreset("local");
		mocks.sent = [];
		chooseEditPreset("api");
		expect(mocks.sent).toEqual([]);
		expect(settings.editPreset).toBeNull();
		expect(roleView().slugs.brain).toBe("vendor/big");
	});
});

describe("saving a role of the chosen model", () => {
	beforeEach(() => {
		chooseEditPreset("local");
		emit(LOCAL_ROLES);
		mocks.sent = [];
	});

	it("names the chosen model in every write", () => {
		saveSlug("brain", "house/model");
		saveTierCredential("worker", "openrouter");
		saveContextWindow("validator", 65536);
		expect(mocks.sent).toEqual([
			{ type: "set_tier_slug", preset: "local", tier: "brain", slug: "house/model" },
			{ type: "set_tier_credential", preset: "local", tier: "worker", credential: "openrouter" },
			{ type: "set_tier_context_window", preset: "local", tier: "validator", context_window: 65536 },
		]);
	});

	it("compares a window with the chosen model's, not the active one's", () => {
		saveContextWindow("brain", 32768);
		expect(mocks.sent).toEqual([]);
		saveContextWindow("brain", 200000);
		expect(mocks.sent).toHaveLength(1);
	});

	it("reads the chosen model again after the daemon acks a write", () => {
		saveContextWindow("brain", 65536);
		mocks.sent = [];
		emit(setupState());
		expect(mocks.sent).toEqual([{ type: "get_preset_roles", name: "local" }]);
		expect(editTarget()).toBe("local");
	});

	it("lets go of a chosen model that was removed, or became the active one", () => {
		emit(setupState({ presets: ["api", "grok"] }));
		expect(settings.editPreset).toBeNull();
		chooseEditPreset("grok");
		emit(setupState({ active_preset: "grok" }));
		expect(settings.editPreset).toBeNull();
	});
});

describe("Settings → Model", () => {
	function open(): HTMLElement {
		const target = document.createElement("div");
		document.body.appendChild(target);
		component = mount(SettingsModels, { target });
		flushSync();
		mocks.sent = [];
		return target;
	}

	function choose(root: HTMLElement, name: string): void {
		const select = root.querySelector<HTMLSelectElement>('select[aria-label="Model to edit"]')!;
		select.value = name;
		select.dispatchEvent(new Event("change", { bubbles: true }));
		flushSync();
	}

	it("offers every model to edit, starting on the one new chats use", () => {
		const root = open();
		const select = root.querySelector<HTMLSelectElement>('select[aria-label="Model to edit"]')!;
		expect([...select.options].map((o) => [o.value, o.textContent])).toEqual([
			["api", "Hosted"],
			["grok", "Grok"],
			["local", "Laptop"],
		]);
		expect(select.value).toBe("api");
		const brain = root.querySelector<HTMLInputElement>('input[aria-label="Brain model"]')!;
		expect(brain.value).toBe("vendor/big");
	});

	it("shows the chosen model's roles and switches nothing", () => {
		const root = open();
		choose(root, "local");
		expect(mocks.sent).toEqual([{ type: "get_preset_roles", name: "local" }]);
		emit(LOCAL_ROLES);
		const brain = root.querySelector<HTMLInputElement>('input[aria-label="Brain model"]')!;
		expect(brain.value).toBe("");
		expect(brain.placeholder).toBe("discovered from the endpoint");
		expect(root.textContent).toContain("Active model Hosted");
		expect(mocks.sent.some((m) => m.type === "set_preset" || m.type === "switch_preset")).toBe(
			false,
		);
	});

	it("says a Grok model has no roles to set", () => {
		const root = open();
		choose(root, "grok");
		emit({ ...LOCAL_ROLES, preset: "grok", tier_slugs: {}, tier_cards: {} } as DaemonEventUnion);
		expect(root.textContent).toContain("Grok runs on the Grok CLI");
		expect(root.querySelector('input[aria-label="Brain model"]')).toBeNull();
	});

	it("has no chooser when there is one model", () => {
		emit(setupState({ presets: ["api"], preset_labels: { api: "Hosted" }, preset_engines: {} }));
		const root = open();
		expect(root.querySelector('select[aria-label="Model to edit"]')).toBeNull();
	});
});
