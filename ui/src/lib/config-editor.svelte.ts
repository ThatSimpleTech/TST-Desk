// Settings → Configuration store (TD-5203).
//
// Holds the daemon's last config_editor view and one save state per key.
// Edits go out one at a time, in order: each carries the version of the
// view it was made against, and the daemon refuses an edit through a list
// position once the file changed, so a second edit sent before the first
// answer could be refused for the first one's own write. A refused edit
// leaves the store's values as the daemon last sent them, which is what
// puts a control back. Sent only via connection-status, like the other
// settings stores.

import { onConnectionState, onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { pathKey, type ConfigPath } from "./config-editor.js";
import type { ConfigEditor, ConfigModelDef, DaemonEventUnion, JsonValue } from "./protocol";

export type FieldSave = { state: "saving" | "saved" | "error"; message: string };

export type EditorStatus = "idle" | "loading" | "ready" | "computer_only" | "error";

export const configEditor = $state({
	status: "idle" as EditorStatus,
	error: "",
	root: "",
	defs: {} as Record<string, ConfigModelDef>,
	values: {} as Record<string, JsonValue>,
	/** Path keys the file itself sets. */
	setPaths: {} as Record<string, true>,
	defaults: {} as Record<string, JsonValue>,
	version: "",
	file: "",
	/** Save state per path key; a key with no entry is at rest. */
	saves: {} as Record<string, FieldSave>,
	query: "",
	/** Expanded sections and groups, by path key. */
	open: {} as Record<string, boolean>,
	/** Moves on every answer, so a control showing a refused edit redraws. */
	revision: 0,
});

type Edit = { kind: "set" | "reset"; path: ConfigPath; value: JsonValue };

/** How long an edit may wait for its answer before the field says so. */
export const EDIT_TIMEOUT_MS = 15_000;
/** How long "Saved" stays under a field. */
export const SAVED_SHOWN_MS = 2_500;

let queue: Edit[] = [];
let inFlight: Edit | null = null;
let timeout: ReturnType<typeof setTimeout> | null = null;
let started = false;

/** Register the reducer once; reloads after a reconnect. Returns the unsubscribe for tests. */
export function startConfigEditor(): () => void {
	if (started) return () => {};
	started = true;
	const offEvents = onEvent(reduce);
	const offState = onConnectionState((state) => {
		if (state !== "connected") {
			failInFlight("The connection dropped before the daemon answered.");
			return;
		}
		if (configEditor.status !== "idle") loadConfigEditor();
	});
	return () => {
		started = false;
		offEvents();
		offState();
	};
}

/** Ask for the whole view. The current one stays on screen until it lands. */
export function loadConfigEditor(): void {
	if (configEditor.status !== "ready") configEditor.status = "loading";
	sendToDaemon({ type: "get_config_editor" });
}

export function saveConfigValue(path: ConfigPath, value: JsonValue): void {
	enqueue({ kind: "set", path, value });
}

/** Back to the default; for a list item or map entry, remove it. */
export function resetConfigValue(path: ConfigPath): void {
	enqueue({ kind: "reset", path, value: null });
}

export function isOpen(path: ConfigPath): boolean {
	return configEditor.open[pathKey(path)] === true;
}

export function setOpen(path: ConfigPath, open: boolean): void {
	configEditor.open[pathKey(path)] = open;
}

/** Reset for tests. Does not touch the daemon. */
export function resetConfigEditor(): void {
	configEditor.status = "idle";
	configEditor.error = "";
	configEditor.root = "";
	configEditor.defs = {};
	configEditor.values = {};
	configEditor.setPaths = {};
	configEditor.defaults = {};
	configEditor.version = "";
	configEditor.file = "";
	configEditor.saves = {};
	configEditor.query = "";
	configEditor.open = {};
	configEditor.revision = 0;
	queue = [];
	inFlight = null;
	if (timeout !== null) clearTimeout(timeout);
	timeout = null;
}

function enqueue(edit: Edit): void {
	configEditor.saves[pathKey(edit.path)] = { state: "saving", message: "" };
	queue.push(edit);
	pump();
}

function pump(): void {
	if (inFlight !== null) return;
	const next = queue.shift();
	if (next === undefined) return;
	inFlight = next;
	const version = configEditor.version;
	const sent =
		next.kind === "set"
			? sendToDaemon({ type: "set_config_value", path: next.path, value: next.value, version })
			: sendToDaemon({ type: "reset_config_value", path: next.path, version });
	if (!sent) {
		failInFlight("Not connected to the daemon.");
		return;
	}
	timeout = setTimeout(() => failInFlight("The daemon did not answer."), EDIT_TIMEOUT_MS);
}

function settle(state: FieldSave): void {
	if (inFlight === null) return;
	const key = pathKey(inFlight.path);
	const done = inFlight;
	// A later edit of the same key is still to go: the field is still saving.
	const again = queue.some((edit) => samePath(edit.path, done.path));
	configEditor.saves[key] = again && state.state === "saved" ? { state: "saving", message: "" } : state;
	configEditor.revision += 1;
	if (state.state === "saved") {
		setTimeout(() => {
			if (configEditor.saves[key]?.state === "saved") delete configEditor.saves[key];
		}, SAVED_SHOWN_MS);
	}
	inFlight = null;
	if (timeout !== null) clearTimeout(timeout);
	timeout = null;
	pump();
}

function failInFlight(message: string): void {
	settle({ state: "error", message });
}

function samePath(a: ConfigPath, b: ConfigPath): boolean {
	return pathKey(a) === pathKey(b);
}

function apply(event: ConfigEditor): void {
	configEditor.root = event.root;
	// The schema and defaults come with the first view only: they are fixed
	// for the daemon's life, so an edit's answer leaves them out.
	if (Object.keys(event.defs).length > 0) configEditor.defs = event.defs;
	if (Object.keys(event.defaults).length > 0) configEditor.defaults = event.defaults;
	configEditor.values = event.values;
	configEditor.setPaths = Object.fromEntries(event.set_paths.map((p) => [pathKey(p), true as const]));
	configEditor.version = event.version;
	configEditor.file = event.file;
	configEditor.status = "ready";
	configEditor.error = "";
	configEditor.revision += 1;
}

function reduce(event: DaemonEventUnion): void {
	switch (event.type) {
		case "config_editor":
			apply(event);
			if (event.edit !== null && inFlight !== null && samePath(event.edit.path, inFlight.path)) {
				// The pages that show some of these keys (Model, MCP servers…) read
				// setup_state; ask before the next edit goes, so it is current first.
				sendToDaemon({ type: "get_setup_state" });
				settle({ state: "saved", message: "" });
			}
			break;
		case "config_value_rejected":
			if (inFlight !== null && samePath(event.path, inFlight.path)) {
				settle({ state: "error", message: event.message });
			}
			if (event.code === "stale") loadConfigEditor();
			break;
		case "error":
			if (event.in_reply_to === "get_config_editor") {
				configEditor.status = event.code === "loopback_only" ? "computer_only" : "error";
				configEditor.error = event.message;
			} else if (
				inFlight !== null &&
				(event.in_reply_to === "set_config_value" || event.in_reply_to === "reset_config_value")
			) {
				failInFlight(event.message);
			}
			break;
	}
}
