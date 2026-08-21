import { describe, expect, it, beforeEach, vi } from "vitest";
import { contextEmptyCopy, filterPins } from "./context-pins";
import type { ClientMessageUnion, ContextPinEntry, DaemonEventUnion } from "./protocol";

const connection = vi.hoisted(() => {
	const handlers = new Set<(event: unknown) => void>();
	return {
		handlers,
		send: vi.fn((_msg: unknown) => true),
	};
});

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (event: unknown) => void) => {
		connection.handlers.add(handler);
		return () => {
			connection.handlers.delete(handler);
		};
	},
	sendToDaemon: connection.send,
}));

import {
	addContextPin,
	contextPins,
	loadContextPins,
	removeContextPin,
	resetContextPins,
	setPinQuery,
	startContextPins,
} from "./context-pins.svelte.js";

function pin(overrides: Partial<ContextPinEntry> = {}): ContextPinEntry {
	return { path: "src/app.ts", name: "app.ts", kind: "file", lines: 12, ...overrides };
}

function emit(event: DaemonEventUnion): void {
	for (const handler of connection.handlers) handler(event);
}

describe("filterPins", () => {
	const pins = [pin(), pin({ path: "docs/readme.md", name: "readme.md", lines: 4 })];

	it("filters locally and never implies a web fetch", () => {
		expect(filterPins(pins, "readme").map((p) => p.name)).toEqual(["readme.md"]);
	});

	it("empty query keeps every pin", () => {
		expect(filterPins(pins, "")).toHaveLength(2);
	});
});

describe("copy", () => {
	it("does not mention sync or the web", () => {
		expect(contextEmptyCopy().toLowerCase()).not.toMatch(/sync|http|web/);
	});
});

describe("store", () => {
	beforeEach(() => {
		connection.send.mockClear();
		connection.handlers.clear();
		resetContextPins();
		startContextPins();
	});

	it("loads pins for a workspace", () => {
		loadContextPins("/ws");
		expect(connection.send).toHaveBeenCalledWith({ type: "list_pins", workspace_path: "/ws" });
		emit({
			type: "context_pins",
			seq: 1,
			workspace_path: "/ws",
			pins: [pin()],
		});
		expect(contextPins.pins.map((p) => p.path)).toEqual(["src/app.ts"]);
	});

	it("add and unpin send human-path messages", () => {
		loadContextPins("/ws");
		addContextPin("/ws/src/app.ts");
		removeContextPin("src/app.ts");
		expect(connection.send).toHaveBeenCalledWith({
			type: "add_pin",
			workspace_path: "/ws",
			path: "/ws/src/app.ts",
		});
		expect(connection.send).toHaveBeenCalledWith({
			type: "remove_pin",
			workspace_path: "/ws",
			path: "src/app.ts",
		});
	});

	it("keeps a search query without sending it", () => {
		loadContextPins("/ws");
		connection.send.mockClear();
		setPinQuery("app");
		expect(contextPins.query).toBe("app");
		expect(connection.send).not.toHaveBeenCalled();
	});
});
