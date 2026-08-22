// Tests for the slash-command store (TD-4501).
//
// The store touches the daemon only through connection-status's
// sendToDaemon/onEvent, so the tests mock exactly that seam and drive the
// `commands` event the daemon replies with. Nothing here needs a DOM.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, CommandEntry, DaemonEventUnion } from "./protocol";

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
	matchCommands,
	requestCommands,
	resetSlashCommands,
	slashCommands,
	startSlashCommands,
} from "./commands.svelte.js";

function command(over: Partial<CommandEntry> = {}): CommandEntry {
	return { name: "deploy", source: "workspace", path: "/w/.tst/commands/deploy.md", fallback: false, ...over };
}

function commandsEvent(items: CommandEntry[]): DaemonEventUnion {
	return { type: "commands", seq: 1, workspace_path: "/w", commands: items };
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	resetSlashCommands();
});

describe("reduce", () => {
	it("replaces the listing on a commands event", () => {
		startSlashCommands();
		expect(slashCommands.items).toEqual([]);
		mocks.handler?.(commandsEvent([command(), command({ name: "review", source: "user" })]));
		expect(slashCommands.items.map((c) => c.name)).toEqual(["deploy", "review"]);
		mocks.handler?.(commandsEvent([command({ name: "one" })]));
		expect(slashCommands.items.map((c) => c.name)).toEqual(["one"]);
	});

	it("ignores every other event", () => {
		startSlashCommands();
		mocks.handler?.(commandsEvent([command()]));
		mocks.handler?.({ type: "policy_rules", seq: 2, rules: [] });
		expect(slashCommands.items).toHaveLength(1);
	});
});

describe("requestCommands", () => {
	it("sends list_commands keyed on the workspace", () => {
		requestCommands("/w");
		expect(mocks.sent).toEqual([{ type: "list_commands", workspace_path: "/w" }]);
	});

	it("clears instead of asking when no workspace is open", () => {
		startSlashCommands();
		mocks.handler?.(commandsEvent([command()]));
		requestCommands(null);
		expect(slashCommands.items).toEqual([]);
		expect(mocks.sent).toEqual([]);
	});
});

describe("matchCommands", () => {
	const items = [command(), command({ name: "Deploy-All" }), command({ name: "review", source: "user" })];

	it("filters case-insensitively on the prefix", () => {
		expect(matchCommands(items, "dep").map((c) => c.name)).toEqual(["deploy", "Deploy-All"]);
		expect(matchCommands(items, "DEP").map((c) => c.name)).toEqual(["deploy", "Deploy-All"]);
		expect(matchCommands(items, "rev").map((c) => c.name)).toEqual(["review"]);
	});

	it("returns a copy of everything for an empty query", () => {
		const matched = matchCommands(items, "");
		expect(matched).toHaveLength(3);
		expect(matched).not.toBe(items);
	});
});
