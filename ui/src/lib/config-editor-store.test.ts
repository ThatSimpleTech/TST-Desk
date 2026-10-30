// TD-5203: the Settings → Configuration store. Edits go out one at a time
// with the view's version; an answer settles the field it was for; a
// refusal leaves the values as the daemon sent them; a phone is told the
// page is the computer's own.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, ConfigEditor, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
	handlers: [] as ((e: DaemonEventUnion) => void)[],
	states: [] as ((s: string) => void)[],
	connected: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (h: (e: DaemonEventUnion) => void) => (mocks.handlers.push(h), () => undefined),
	onConnectionState: (h: (s: string) => void) => (mocks.states.push(h), () => undefined),
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.connected ? (mocks.sent.push(msg), true) : false),
}));

import {
	configEditor,
	EDIT_TIMEOUT_MS,
	loadConfigEditor,
	resetConfigEditor,
	resetConfigValue,
	saveConfigValue,
	startConfigEditor,
} from "./config-editor.svelte.js";

let stop: () => void = () => undefined;

function emit(event: object): void {
	for (const handler of mocks.handlers) handler(event as unknown as DaemonEventUnion);
}

function view(over: Partial<ConfigEditor> = {}): ConfigEditor {
	return {
		type: "config_editor",
		seq: 1,
		root: "ModelConfig",
		defs: { ModelConfig: { name: "ModelConfig", fields: [], add_fields: [], empty_ok: true } },
		values: { workflow: { max_parallel: 3 } },
		set_paths: [["workflow"], ["workflow", "max_parallel"]],
		defaults: { '["workflow","max_parallel"]': 3 },
		version: "v1",
		file: "/data/config.yaml",
		edit: null,
		...over,
	};
}

const PARALLEL = ["workflow", "max_parallel"];
const KEY = JSON.stringify(PARALLEL);

beforeEach(() => {
	vi.useFakeTimers();
	mocks.sent = [];
	mocks.handlers = [];
	mocks.states = [];
	mocks.connected = true;
	resetConfigEditor();
	stop = startConfigEditor();
});

afterEach(() => {
	stop();
	vi.useRealTimers();
});

describe("config editor store", () => {
	it("asks for the view and takes it in", () => {
		loadConfigEditor();
		expect(mocks.sent).toEqual([{ type: "get_config_editor" }]);
		expect(configEditor.status).toBe("loading");
		emit(view());
		expect(configEditor.status).toBe("ready");
		expect(configEditor.values).toEqual({ workflow: { max_parallel: 3 } });
		expect(configEditor.setPaths[KEY]).toBe(true);
		expect(configEditor.version).toBe("v1");
	});

	it("keeps the schema and defaults when an edit's answer leaves them out", () => {
		emit(view());
		saveConfigValue(PARALLEL, 5);
		emit(view({ defs: {}, defaults: {}, values: { workflow: { max_parallel: 5 } }, version: "v2", edit: { action: "set", path: PARALLEL } }));
		expect(Object.keys(configEditor.defs)).toEqual(["ModelConfig"]);
		expect(configEditor.defaults[KEY]).toBe(3);
		expect(configEditor.values).toEqual({ workflow: { max_parallel: 5 } });
	});

	it("sends one edit at a time, each with the latest version", () => {
		emit(view());
		saveConfigValue(PARALLEL, 5);
		resetConfigValue(["hooks", "after_write", 0]);
		expect(mocks.sent).toEqual([{ type: "set_config_value", path: PARALLEL, value: 5, version: "v1" }]);
		expect(configEditor.saves[KEY]).toEqual({ state: "saving", message: "" });
		emit(view({ version: "v2", edit: { action: "set", path: PARALLEL } }));
		expect(configEditor.saves[KEY]).toEqual({ state: "saved", message: "" });
		expect(mocks.sent.slice(1)).toEqual([
			{ type: "get_setup_state" },
			{ type: "reset_config_value", path: ["hooks", "after_write", 0], version: "v2" },
		]);
		vi.advanceTimersByTime(3_000);
		expect(configEditor.saves[KEY]).toBeUndefined();
	});

	it("shows a refusal on its field and keeps the stored value", () => {
		emit(view());
		saveConfigValue(PARALLEL, 99);
		emit({
			type: "config_value_rejected",
			seq: 1,
			action: "set",
			path: PARALLEL,
			code: "invalid",
			message: "workflow.max_parallel: Input should be less than or equal to 16",
		});
		expect(configEditor.saves[KEY]).toEqual({
			state: "error",
			message: "workflow.max_parallel: Input should be less than or equal to 16",
		});
		expect(configEditor.values).toEqual({ workflow: { max_parallel: 3 } });
	});

	it("reloads the view when the file changed under an edit", () => {
		emit(view());
		resetConfigValue(["hooks", "after_write", 0]);
		mocks.sent = [];
		emit({ type: "config_value_rejected", seq: 1, action: "reset", path: ["hooks", "after_write", 0], code: "stale", message: "changed" });
		expect(mocks.sent).toEqual([{ type: "get_config_editor" }]);
	});

	it("says the page is the computer's own when a phone asks", () => {
		loadConfigEditor();
		emit({ type: "error", code: "loopback_only", message: "Only the computer itself can do that.", in_reply_to: "get_config_editor" });
		expect(configEditor.status).toBe("computer_only");
	});

	it("fails an edit the daemon refused outright, and one that never got an answer", () => {
		emit(view());
		saveConfigValue(PARALLEL, 4);
		emit({ type: "error", code: "loopback_only", message: "Only the computer itself can do that.", in_reply_to: "set_config_value" });
		expect(configEditor.saves[KEY]?.state).toBe("error");
		saveConfigValue(["search", "max_results"], 2);
		vi.advanceTimersByTime(EDIT_TIMEOUT_MS + 1);
		expect(configEditor.saves['["search","max_results"]']).toEqual({ state: "error", message: "The daemon did not answer." });
	});

	it("does not wait on an edit it could not send", () => {
		emit(view());
		mocks.connected = false;
		saveConfigValue(PARALLEL, 4);
		expect(configEditor.saves[KEY]).toEqual({ state: "error", message: "Not connected to the daemon." });
	});

	it("keeps a key saving while a later edit of it is still to go", () => {
		emit(view());
		saveConfigValue(PARALLEL, 4);
		saveConfigValue(PARALLEL, 5);
		emit(view({ version: "v2", edit: { action: "set", path: PARALLEL } }));
		expect(configEditor.saves[KEY]?.state).toBe("saving");
		expect(mocks.sent.at(-1)).toEqual({ type: "set_config_value", path: PARALLEL, value: 5, version: "v2" });
	});
});
