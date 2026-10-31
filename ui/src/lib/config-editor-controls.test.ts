// @vitest-environment jsdom
//
// TD-5203: Settings → Configuration controls while saves are out. A word
// list row being typed in keeps its focus and text when another key's save
// is answered; a refused select shows the stored choice again; choices
// stepped through with the keys save once; two quick toggles both count;
// Escape drops a draft without closing Settings; Reset clears a draft that
// never went; a key written through a YAML anchor is shown, not edited.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, ConfigModelDef, ConfigNode, DaemonEventUnion, JsonValue } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
	handlers: [] as ((e: DaemonEventUnion) => void)[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (h: (e: DaemonEventUnion) => void) => (mocks.handlers.push(h), () => undefined),
	onConnectionState: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import ConfigAddForm from "./components/config/ConfigAddForm.svelte";
import ConfigField from "./components/config/ConfigField.svelte";
import { LOCKED_NOTE } from "./config-editor.js";
import { configEditor, resetConfigEditor, saveConfigValue, startConfigEditor } from "./config-editor.svelte.js";

function node(over: Partial<ConfigNode>): ConfigNode {
	return {
		kind: "str", ref: null, nullable: false, title: "", description: "", unit: "",
		default: null, has_default: false, editable: true, resettable: true, multiline: false,
		note: "", minimum: null, maximum: null, exclusive_minimum: null, exclusive_maximum: null,
		min_length: null, max_length: null, choices: [], choices_from: [], items: null,
		entries: null, variants: [], addable: true, removable: true, add_note: "", remove_note: "",
		...over,
	};
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let stop: () => void = () => undefined;

function show(n: ConfigNode, path: (string | number)[], name: string): void {
	component = mount(ConfigField, { target, props: { node: n, path, name } });
	flushSync();
}

function emit(event: Record<string, unknown>): void {
	for (const handler of mocks.handlers) handler(event as unknown as DaemonEventUnion);
	flushSync();
}

/** The daemon's answer to the edit of *path*, with the values it now holds. */
function ack(path: (string | number)[], values: Record<string, JsonValue> = configEditor.values): void {
	emit({ type: "config_editor", seq: 1, root: "ModelConfig", defs: {}, values, set_paths: [],
		locked_paths: [], defaults: {}, version: "v2", file: "", edit: { action: "set", path } });
}

function refuse(path: (string | number)[]): void {
	emit({ type: "config_value_rejected", seq: 1, action: "set", path, code: "invalid", message: "no" });
}

function type(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
	el.value = value;
	el.dispatchEvent(new Event("input", { bubbles: true }));
	flushSync();
}

function key(el: HTMLElement, name: string): void {
	el.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true }));
	flushSync();
}

function edits(): ClientMessageUnion[] {
	return mocks.sent.filter((m) => m.type !== "get_setup_state");
}

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

beforeEach(() => {
	mocks.sent = [];
	mocks.handlers = [];
	resetConfigEditor();
	stop = startConfigEditor();
	configEditor.status = "ready";
	configEditor.root = "ModelConfig";
	configEditor.version = "v1";
	configEditor.values = {
		search: { fallback_base_urls: ["https://one.invalid/", "https://two.invalid/"], timeout_seconds: 15 },
		session: { default_effort: "medium" },
		consult: { tiers: ["worker"] },
		workflow: { max_parallel: 6 },
	};
	configEditor.defaults = { '["workflow","max_parallel"]': 3 };
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	stop();
	target.remove();
	vi.useRealTimers();
});

describe("config controls while saves are out", () => {
	it("a list row keeps its focus and text when another key's save is answered", () => {
		show(node({ kind: "list", items: node({ kind: "str" }) }), ["search", "fallback_base_urls"], "fallback_base_urls");
		saveConfigValue(["search", "timeout_seconds"], 22);
		const row = target.querySelector('input[aria-label="Item 1"]') as HTMLInputElement;
		row.focus();
		type(row, "https://one.invalid/extra");
		ack(["search", "timeout_seconds"], { ...configEditor.values });
		const now = target.querySelector('input[aria-label="Item 1"]') as HTMLInputElement;
		expect(now).toBe(row);
		expect(document.activeElement).toBe(row);
		expect(row.value).toBe("https://one.invalid/extra");
		expect(edits().map((m) => m.type === "set_config_value" && m.path)).toEqual([["search", "timeout_seconds"]]);
	});

	it("a refused select shows the stored choice again", () => {
		show(node({ kind: "enum", choices: EFFORTS }), ["session", "default_effort"], "default_effort");
		const select = target.querySelector("select") as HTMLSelectElement;
		expect(select.value).toBe("1");
		select.value = "3";
		select.dispatchEvent(new Event("change", { bubbles: true }));
		flushSync();
		expect(edits()).toEqual([{ type: "set_config_value", path: ["session", "default_effort"], value: "xhigh", version: "v1" }]);
		refuse(["session", "default_effort"]);
		expect(select.value).toBe("1");
	});

	it("choices stepped through with the keys save once they rest", () => {
		vi.useFakeTimers();
		show(node({ kind: "enum", choices: EFFORTS }), ["session", "default_effort"], "default_effort");
		const select = target.querySelector("select") as HTMLSelectElement;
		for (const index of ["2", "3", "4"]) {
			key(select, "ArrowDown");
			select.value = index;
			select.dispatchEvent(new Event("change", { bubbles: true }));
			flushSync();
		}
		expect(edits()).toEqual([]);
		vi.advanceTimersByTime(1_000);
		flushSync();
		expect(edits()).toEqual([{ type: "set_config_value", path: ["session", "default_effort"], value: "max", version: "v1" }]);
	});

	it("a second toggle before the first is answered keeps the first", () => {
		const items = node({ kind: "enum", choices: ["brain", "worker", "validator"] });
		show(node({ kind: "list", items }), ["consult", "tiers"], "tiers");
		const chip = (label: string) =>
			[...target.querySelectorAll("button")].find((b) => b.textContent?.trim() === label) as HTMLButtonElement;
		chip("brain").click();
		flushSync();
		chip("validator").click();
		flushSync();
		ack(["consult", "tiers"], { ...configEditor.values, consult: { tiers: ["brain", "worker"] } });
		expect(edits().at(-1)).toEqual({
			type: "set_config_value", path: ["consult", "tiers"], value: ["brain", "worker", "validator"], version: "v2",
		});
	});

	it("Escape drops a draft and stays in Settings; with nothing to drop it goes on", () => {
		show(node({ kind: "int", minimum: 1, maximum: 16 }), ["workflow", "max_parallel"], "max_parallel");
		const seen: string[] = [];
		const onWindow = (e: KeyboardEvent) => seen.push(e.key);
		window.addEventListener("keydown", onWindow);
		try {
			const box = target.querySelector("input") as HTMLInputElement;
			type(box, "7");
			key(box, "Escape");
			expect(seen).toEqual([]);
			expect(box.value).toBe("6");
			key(box, "Escape");
			expect(seen).toEqual(["Escape"]);
		} finally {
			window.removeEventListener("keydown", onWindow);
		}
	});

	it("Reset clears a draft that never went, and its reason", () => {
		show(node({ kind: "int", minimum: 1, maximum: 16 }), ["workflow", "max_parallel"], "max_parallel");
		const box = target.querySelector("input") as HTMLInputElement;
		type(box, "99");
		box.dispatchEvent(new Event("blur"));
		flushSync();
		expect(target.textContent).toContain("Use at most 16.");
		(target.querySelector('button[aria-label="Reset max_parallel to default"]') as HTMLButtonElement).click();
		flushSync();
		ack(["workflow", "max_parallel"], { ...configEditor.values, workflow: { max_parallel: 3 } });
		expect((target.querySelector("input") as HTMLInputElement).value).toBe("3");
		expect(target.textContent).not.toContain("Use at most 16.");
	});

	it("names its buttons for their key, and keeps the badge out of the label", () => {
		show(node({ kind: "list", items: node({ kind: "str" }) }), ["search", "fallback_base_urls"], "fallback_base_urls");
		expect(target.querySelector('button[aria-label="Add to fallback_base_urls"]')).not.toBeNull();
		unmount(component as ReturnType<typeof mount>);
		show(node({ kind: "int", minimum: 1, maximum: 16 }), ["workflow", "max_parallel"], "max_parallel");
		expect(target.textContent).toContain("Modified");
		expect((target.querySelector("label") as HTMLLabelElement).textContent).not.toContain("Modified");
	});

	it("a key written through a YAML anchor is shown and locked", () => {
		configEditor.values = { presets: { team: { label: "Team", worker: { context_window: 32768 } } } };
		configEditor.locked = { '["presets","team","label"]': true, '["presets","team","worker"]': true };
		show(node({ kind: "int", minimum: 1 }), ["presets", "team", "worker", "context_window"], "context_window");
		expect((target.querySelector("input") as HTMLInputElement).disabled).toBe(true);
		expect(target.textContent).not.toContain(LOCKED_NOTE);
		unmount(component as ReturnType<typeof mount>);
		show(node({ kind: "str" }), ["presets", "team", "label"], "label");
		expect((target.querySelector("input") as HTMLInputElement).disabled).toBe(true);
		expect(target.textContent).toContain(LOCKED_NOTE);
	});

	it("an add form reads a free-form field as JSON", () => {
		const def: ConfigModelDef = {
			name: "FastConfig",
			fields: [
				{ name: "hook_url", required: false, node: node({ kind: "str", nullable: true }) },
				{ name: "extra_body", required: false, node: node({ kind: "json" }) },
			],
			add_fields: ["hook_url", "extra_body"],
			empty_ok: false,
		};
		const submitted: Record<string, JsonValue>[] = [];
		component = mount(ConfigAddForm, {
			target,
			props: { def, idPrefix: "fast", label: "Turn on fast", onsubmit: (v: Record<string, JsonValue>) => submitted.push(v), oncancel: () => undefined },
		});
		flushSync();
		const body = target.querySelector("textarea") as HTMLTextAreaElement;
		const form = target.querySelector("form") as HTMLFormElement;
		type(body, "{not json");
		form.dispatchEvent(new Event("submit", { cancelable: true }));
		flushSync();
		expect(submitted).toEqual([]);
		expect(target.textContent).toContain("extra_body: That is not valid JSON.");
		type(body, '{"service_tier": "priority"}');
		form.dispatchEvent(new Event("submit", { cancelable: true }));
		flushSync();
		expect(submitted).toEqual([{ extra_body: { service_tier: "priority" } }]);
	});
});
