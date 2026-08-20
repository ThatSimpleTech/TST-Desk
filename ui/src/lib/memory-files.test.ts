// Tests for the Memory column (TD-2601).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { memoryEmptyCopy } from "./memory-files";

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

import {
	beginEdit,
	cancelEdit,
	loadMemoryFiles,
	memoryFiles,
	resetMemoryFiles,
	saveMemoryFile,
	selectMemoryFile,
	selectedMemoryFile,
	setMemoryDraft,
	startMemoryFiles,
} from "./memory-files.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetMemoryFiles();
	startMemoryFiles();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetMemoryFiles();
});

describe("copy", () => {
	it("points the empty state at the first distill", () => {
		expect(memoryEmptyCopy()).toMatch(/End a session/);
	});
});

describe("store", () => {
	it("lists files the daemon reports for this workspace", () => {
		loadMemoryFiles("/ws");
		expect(mocks.sent).toEqual([{ type: "list_memory", workspace_path: "/ws" }]);
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{
					path: "/ws/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "durable: ruff\n",
				},
			],
		});
		expect(memoryFiles.files.map((f) => f.name)).toEqual(["MEMORY.md"]);
		selectMemoryFile("/ws/.tst/memory/MEMORY.md");
		expect(selectedMemoryFile()?.content).toBe("durable: ruff\n");
	});

	it("ignores a list for a different workspace", () => {
		loadMemoryFiles("/ws");
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/other",
			files: [
				{
					path: "/other/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "nope\n",
				},
			],
		});
		expect(memoryFiles.files).toEqual([]);
	});

	it("saves the draft through save_memory", () => {
		loadMemoryFiles("/ws");
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{
					path: "/ws/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "old\n",
				},
			],
		});
		selectMemoryFile("/ws/.tst/memory/MEMORY.md");
		beginEdit();
		setMemoryDraft("durable: ruff\n");
		expect(saveMemoryFile()).toBe(true);
		expect(mocks.sent.at(-1)).toEqual({
			type: "save_memory",
			workspace_path: "/ws",
			path: "MEMORY.md",
			content: "durable: ruff\n",
		});
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{
					path: "/ws/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "durable: ruff\n",
				},
			],
		});
		expect(memoryFiles.editing).toBe(false);
		expect(selectedMemoryFile()?.content).toBe("durable: ruff\n");
	});

	it("keeps the draft when save is refused", () => {
		loadMemoryFiles("/ws");
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{
					path: "/ws/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "old\n",
				},
			],
		});
		selectMemoryFile("/ws/.tst/memory/MEMORY.md");
		beginEdit();
		setMemoryDraft("too long\n");
		saveMemoryFile();
		emit({
			type: "error",
			seq: 1,
			code: "memory_cap",
			message: "this write would make MEMORY.md 201 lines; the cap is 200.",
		});
		expect(memoryFiles.editing).toBe(true);
		expect(memoryFiles.draft).toBe("too long\n");
		expect(memoryFiles.error).toMatch(/cap is 200/);
	});

	it("cancel restores the last listed bytes", () => {
		loadMemoryFiles("/ws");
		emit({
			type: "memory_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{
					path: "/ws/.tst/memory/MEMORY.md",
					name: "MEMORY.md",
					content: "old\n",
				},
			],
		});
		selectMemoryFile("/ws/.tst/memory/MEMORY.md");
		beginEdit();
		setMemoryDraft("scratch\n");
		cancelEdit();
		expect(memoryFiles.editing).toBe(false);
		expect(selectedMemoryFile()?.content).toBe("old\n");
	});
});
