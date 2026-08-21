// Project home is wired, not a mock (TD-2808).
//
// One workspace, three columns. The daemon events are the source of
// truth; the stores only expose what they were given.

import { describe, expect, it, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const connection = vi.hoisted(() => {
	const handlers = new Set<(event: DaemonEventUnion) => void>();
	return {
		handlers,
		send: vi.fn((_msg: ClientMessageUnion) => true),
	};
});

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (event: DaemonEventUnion) => void) => {
		connection.handlers.add(handler);
		return () => {
			connection.handlers.delete(handler);
		};
	},
	onDaemonEvent: (handler: (event: DaemonEventUnion) => void) => {
		connection.handlers.add(handler);
		return () => {
			connection.handlers.delete(handler);
		};
	},
	sendToDaemon: connection.send,
}));

import { instructions, loadInstructions, resetInstructions, startInstructions } from "./instructions.svelte.js";
import { loadMemoryFiles, memoryFiles, resetMemoryFiles, startMemoryFiles } from "./memory-files.svelte.js";
import { contextPins, loadContextPins, resetContextPins, startContextPins } from "./context-pins.svelte.js";

function emit(event: DaemonEventUnion): void {
	for (const handler of connection.handlers) handler(event);
}

describe("three columns bind one workspace", () => {
	beforeEach(() => {
		connection.send.mockClear();
		connection.handlers.clear();
		resetInstructions();
		resetMemoryFiles();
		resetContextPins();
		startInstructions();
		startMemoryFiles();
		startContextPins();
	});

	it("exposes steering, memory, and pin paths for the bound folder", () => {
		const ws = "/ws/desk";
		loadInstructions(ws);
		loadMemoryFiles(ws);
		loadContextPins(ws);
		emit({
			type: "instruction_files",
			seq: 1,
			workspace_path: ws,
			files: [{ path: `${ws}/AGENTS.md`, name: "AGENTS.md", kind: "agents" }],
		});
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: ws,
			files: [{ path: `${ws}/.tst/memory/MEMORY.md`, name: "MEMORY.md", content: "notes\n" }],
		});
		emit({
			type: "context_pins",
			seq: 1,
			workspace_path: ws,
			pins: [{ path: "src/app.ts", name: "app.ts", kind: "file", lines: 12 }],
		});
		expect(instructions.files.map((f) => f.name)).toEqual(["AGENTS.md"]);
		expect(memoryFiles.files.map((f) => f.name)).toEqual(["MEMORY.md"]);
		expect(contextPins.pins.map((p) => p.path)).toEqual(["src/app.ts"]);
	});
});
