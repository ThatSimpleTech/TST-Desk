// Grok engine UI store: slash filter, plan/preview pane, mode, and the
// client verbs that go to the daemon. The daemon still owns the session.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { resetRightPane, rightPane } from "./right-pane.svelte.js";

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
	approveGrokPlan,
	grok,
	matchingGrokCommands,
	openInTerminal,
	resetGrok,
	runGrokCommand,
	setGrokMode,
	startGrok,
} from "./grok.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetGrok();
	resetRightPane();
	startGrok();
});

afterEach(() => {
	resetGrok();
	resetRightPane();
});

describe("startGrok", () => {
	it("asks the daemon for TUI sessions and extensions", () => {
		expect(mocks.sent).toEqual([
			{ type: "list_grok_sessions" },
			{ type: "list_grok_extensions" },
		]);
	});
});

describe("events", () => {
	it("keeps advertised slash commands", () => {
		emit({
			type: "grok_commands",
			seq: 1,
			session_id: "s1",
			commands: [
				{ name: "compact", description: "Compress context" },
				{ name: "plan", description: "Enter plan mode" },
			],
		});
		expect(grok.commands.map((c) => c.name)).toEqual(["compact", "plan"]);
	});

	it("opens the plan tab on a plan event", () => {
		emit({
			type: "grok_plan",
			seq: 1,
			session_id: "s1",
			markdown: "# Do it",
			entries: [{ content: "Write tests", status: "pending" }],
		});
		expect(grok.planMarkdown).toBe("# Do it");
		expect(grok.planEntries[0]?.content).toBe("Write tests");
		expect(rightPane.tab).toBe("plan");
	});

	it("records the current mode and advertised set", () => {
		emit({
			type: "grok_mode",
			seq: 1,
			session_id: "s1",
			mode: "plan",
			modes: ["agent", "plan"],
		});
		expect(grok.mode).toBe("plan");
		expect(grok.modes).toEqual(["agent", "plan"]);
	});

	it("opens the preview tab on a preview event", () => {
		emit({
			type: "grok_preview",
			seq: 1,
			session_id: "s1",
			kind: "url",
			url: "http://127.0.0.1:5173/",
			title: "app",
		});
		expect(grok.preview?.kind).toBe("url");
		expect(grok.preview?.url).toBe("http://127.0.0.1:5173/");
		expect(rightPane.tab).toBe("preview");
	});

	it("keeps TUI sessions and extensions from connection events", () => {
		emit({
			type: "grok_session_list",
			seq: 1,
			sessions: [{ id: "abc", title: "Fix login", cwd: "/tmp/proj" }],
		});
		emit({
			type: "grok_extensions",
			seq: 1,
			items: [{ kind: "skill", name: "review", detail: "user" }],
		});
		expect(grok.sessions[0]?.title).toBe("Fix login");
		expect(grok.extensions[0]?.name).toBe("review");
	});
});

describe("matchingGrokCommands", () => {
	beforeEach(() => {
		emit({
			type: "grok_commands",
			seq: 1,
			session_id: "s1",
			commands: [
				{ name: "compact", description: "Compress context" },
				{ name: "commit", description: "Write a commit" },
				{ name: "plan", description: "Enter plan mode" },
			],
		});
	});

	it("strips a leading slash and matches a prefix", () => {
		expect(matchingGrokCommands("/com").map((c) => c.name)).toEqual(["compact", "commit"]);
	});

	it("matches a description when the name does not prefix", () => {
		expect(matchingGrokCommands("enter").map((c) => c.name)).toEqual(["plan"]);
	});

	it("returns the advertised list on a bare slash", () => {
		expect(matchingGrokCommands("/").map((c) => c.name)).toEqual(["compact", "commit", "plan"]);
	});
});

describe("client verbs", () => {
	beforeEach(() => {
		mocks.sent.length = 0;
	});

	it("sends set_grok_mode", () => {
		setGrokMode("s1", "plan");
		expect(mocks.sent).toEqual([{ type: "set_grok_mode", session_id: "s1", mode: "plan" }]);
	});

	it("sends run_grok_command", () => {
		runGrokCommand("s1", "compact", "keep tests");
		expect(mocks.sent).toEqual([
			{ type: "run_grok_command", session_id: "s1", name: "compact", argument: "keep tests" },
		]);
	});

	it("sends approve_grok_plan", () => {
		approveGrokPlan("s1", "go");
		expect(mocks.sent).toEqual([
			{ type: "approve_grok_plan", session_id: "s1", comment: "go" },
		]);
	});

	it("sends open_in_terminal", () => {
		openInTerminal("s1");
		expect(mocks.sent).toEqual([{ type: "open_in_terminal", session_id: "s1" }]);
	});
});
