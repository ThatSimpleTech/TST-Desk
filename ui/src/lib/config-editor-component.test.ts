// @vitest-environment jsdom
//
// TD-5203: Settings → Configuration fields in the DOM. A switch saves on
// click; a number box keeps a draft that does not parse on screen and sends
// nothing; a refused save shows the daemon's words and the stored value
// again; a word list saves whole; Reset appears only for a changed key; a
// secrets map shows names and where each lives, never a value.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, ConfigNode, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
	handlers: [] as ((e: DaemonEventUnion) => void)[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (h: (e: DaemonEventUnion) => void) => (mocks.handlers.push(h), () => undefined),
	onConnectionState: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import ConfigField from "./components/config/ConfigField.svelte";
import { configEditor, resetConfigEditor, startConfigEditor } from "./config-editor.svelte.js";

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

function button(label: string): HTMLButtonElement {
	const found = [...target.querySelectorAll("button")].find(
		(b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === label,
	);
	if (!found) throw new Error(`no button ${label}`);
	return found;
}

function type(el: HTMLInputElement, value: string): void {
	el.value = value;
	el.dispatchEvent(new Event("input", { bubbles: true }));
	flushSync();
}

function key(el: HTMLElement, name: string): void {
	el.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true }));
	flushSync();
}

beforeEach(() => {
	mocks.sent = [];
	mocks.handlers = [];
	resetConfigEditor();
	stop = startConfigEditor();
	configEditor.status = "ready";
	configEditor.root = "ModelConfig";
	configEditor.version = "v1";
	configEditor.values = {
		session: { default_ultracode: false },
		workflow: { max_parallel: 3 },
		consult: { exclude: ["shell"] },
		mcp: { servers: { docs: { env: { DOCS_TOKEN: "keychain", PLAIN: "config" } } } },
	};
	configEditor.defaults = { '["workflow","max_parallel"]': 3, '["session","default_ultracode"]': false };
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	stop();
	target.remove();
});

describe("config fields", () => {
	it("a switch saves on click and is tied to its label", () => {
		show(node({ kind: "bool", description: "Start with Ultracode on." }), ["session", "default_ultracode"], "default_ultracode");
		const toggle = target.querySelector('button[role="switch"]') as HTMLButtonElement;
		const label = target.querySelector("label") as HTMLLabelElement;
		expect(label.htmlFor).toBe(toggle.id);
		expect(toggle.getAttribute("aria-checked")).toBe("false");
		toggle.click();
		flushSync();
		expect(mocks.sent).toEqual([
			{ type: "set_config_value", path: ["session", "default_ultracode"], value: true, version: "v1" },
		]);
		expect(target.textContent).toContain("Saving…");
	});

	it("a number box keeps a bad draft on screen, sends a good one, and reverts on refusal", () => {
		show(node({ kind: "int", minimum: 1, maximum: 16, unit: "workers" }), ["workflow", "max_parallel"], "max_parallel");
		const box = target.querySelector("input") as HTMLInputElement;
		expect(target.textContent).toContain("Range: 1 to 16.");
		type(box, "40");
		key(box, "Enter");
		expect(target.textContent).toContain("Use at most 16.");
		expect(mocks.sent).toEqual([]);
		type(box, "12");
		key(box, "Enter");
		expect(mocks.sent).toEqual([
			{ type: "set_config_value", path: ["workflow", "max_parallel"], value: 12, version: "v1" },
		]);
		expect(box.value).toBe("12");
		emit({
			type: "config_value_rejected", seq: 1, action: "set", path: ["workflow", "max_parallel"],
			code: "invalid", message: "the worker's server cannot take that many",
		});
		expect(target.textContent).toContain("the worker's server cannot take that many");
		expect((target.querySelector("input") as HTMLInputElement).value).toBe("3");
	});

	it("a word list saves whole on add and remove", () => {
		show(node({ kind: "list", items: node({ kind: "str" }) }), ["consult", "exclude"], "exclude");
		const add = target.querySelector('input[placeholder="Add an item"]') as HTMLInputElement;
		type(add, "fs_read");
		key(add, "Enter");
		expect(mocks.sent.at(-1)).toEqual({
			type: "set_config_value", path: ["consult", "exclude"], value: ["shell", "fs_read"], version: "v1",
		});
		button("Remove shell").click();
		flushSync();
		// The first edit is still out; the second waits its turn.
		expect(mocks.sent).toHaveLength(1);
		emit({ type: "config_editor", seq: 1, root: "ModelConfig", defs: {}, values: configEditor.values,
			set_paths: [], defaults: {}, version: "v2", file: "", edit: { action: "set", path: ["consult", "exclude"] } });
		expect(mocks.sent.at(-1)).toEqual({
			type: "set_config_value", path: ["consult", "exclude"], value: [], version: "v2",
		});
	});

	it("offers Reset only when the key differs from its default", () => {
		show(node({ kind: "int", minimum: 1, maximum: 16 }), ["workflow", "max_parallel"], "max_parallel");
		expect(target.querySelector('button[aria-label="Reset max_parallel to default"]')).toBeNull();
		configEditor.values = { workflow: { max_parallel: 6 } };
		flushSync();
		expect(target.textContent).toContain("Modified");
		button("Reset max_parallel to default").click();
		flushSync();
		expect(mocks.sent).toEqual([
			{ type: "reset_config_value", path: ["workflow", "max_parallel"], version: "v1" },
		]);
	});

	it("a secrets map names its entries and never holds a value", () => {
		show(
			node({ kind: "secrets", editable: false, note: "Values are set in Settings → MCP servers." }),
			["mcp", "servers", "docs", "env"],
			"env",
		);
		expect(target.textContent).toContain("DOCS_TOKEN");
		expect(target.textContent).toContain("in the keychain");
		expect(target.textContent).toContain("set in config.yaml");
		expect(target.textContent).toContain("Values are set in Settings → MCP servers.");
		expect(target.querySelector("input")).toBeNull();
	});
});
