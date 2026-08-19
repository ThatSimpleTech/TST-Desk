// Tests for the Instructions column (TD-2802).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { instructionEmptyCopy, rootInstruction, ruleInstructions } from "./instructions";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	opened: [] as string[],
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

vi.mock("./open-file", () => ({
	openInEditor: (path: string) => {
		mocks.opened.push(path);
		return Promise.resolve(true);
	},
}));

import {
	instructions,
	startInstructions,
	resetInstructions,
	loadInstructions,
	beginNewRule,
	createRule,
	setDraftName,
} from "./instructions.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	mocks.sent.length = 0;
	mocks.opened.length = 0;
	resetInstructions();
	startInstructions();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetInstructions();
});

describe("copy", () => {
	it("points the empty state at the steering guide", () => {
		expect(instructionEmptyCopy()).toMatch(/docs\/steering\.md/);
	});

	it("splits the root file from rules", () => {
		const files = [
			{ path: "/ws/AGENTS.md", name: "AGENTS.md", kind: "agents" as const },
			{ path: "/ws/.tst/rules/api.md", name: "api.md", kind: "rule" as const },
		];
		expect(rootInstruction(files)?.name).toBe("AGENTS.md");
		expect(ruleInstructions(files).map((f) => f.name)).toEqual(["api.md"]);
	});
});

describe("store", () => {
	it("lists files the daemon reports for this workspace", () => {
		loadInstructions("/ws");
		expect(mocks.sent).toEqual([{ type: "list_instructions", workspace_path: "/ws" }]);
		emit({
			type: "instruction_files",
			seq: 1,
			workspace_path: "/ws",
			files: [{ path: "/ws/AGENTS.md", name: "AGENTS.md", kind: "agents" }],
			created: null,
		});
		expect(instructions.files.map((f) => f.name)).toEqual(["AGENTS.md"]);
	});

	it("ignores a list for a different workspace", () => {
		loadInstructions("/ws");
		emit({
			type: "instruction_files",
			seq: 1,
			workspace_path: "/other",
			files: [{ path: "/other/AGENTS.md", name: "AGENTS.md", kind: "agents" }],
			created: null,
		});
		expect(instructions.files).toEqual([]);
	});

	it("create_rule is a client message, not a tool call", () => {
		loadInstructions("/ws");
		mocks.sent.length = 0;
		beginNewRule();
		setDraftName("api");
		expect(createRule()).toBe(true);
		expect(mocks.sent).toEqual([
			{ type: "create_rule", workspace_path: "/ws", name: "api" },
		]);
		emit({
			type: "instruction_files",
			seq: 1,
			workspace_path: "/ws",
			files: [
				{ path: "/ws/AGENTS.md", name: "AGENTS.md", kind: "agents" },
				{ path: "/ws/.tst/rules/api.md", name: "api.md", kind: "rule" },
			],
			created: "/ws/.tst/rules/api.md",
		});
		expect(instructions.files.map((f) => f.kind)).toEqual(["agents", "rule"]);
		expect(mocks.opened).toEqual(["/ws/.tst/rules/api.md"]);
	});
});
