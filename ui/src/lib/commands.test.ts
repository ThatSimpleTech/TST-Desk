// Tests for the slash-command and skill store (TD-4501, TD-4502).
//
// The store touches the daemon only through connection-status's
// sendToDaemon/onEvent, so the tests mock exactly that seam and drive the
// `commands`/`skills` events the daemon replies with. Nothing here needs a
// DOM.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type {
	ClientMessageUnion,
	CommandEntry,
	DaemonEventUnion,
	SkillSummary,
} from "./protocol";

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
	matchSkills,
	requestCommands,
	resetSlashCommands,
	slashCommands,
	startSlashCommands,
} from "./commands.svelte.js";

function command(over: Partial<CommandEntry> = {}): CommandEntry {
	return { name: "deploy", source: "workspace", path: "/w/.tst/commands/deploy.md", fallback: false, ...over };
}

function skill(over: Partial<SkillSummary> = {}): SkillSummary {
	return {
		name: "review",
		source: "workspace",
		fallback: false,
		description: "read a PR like a reviewer",
		when_to_use: "before requesting review",
		...over,
	};
}

function commandsEvent(items: CommandEntry[]): DaemonEventUnion {
	return { type: "commands", seq: 1, workspace_path: "/w", commands: items };
}

function skillsEvent(items: SkillSummary[]): DaemonEventUnion {
	return { type: "skills", seq: 1, workspace_path: "/w", skills: items };
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

	it("replaces the skill catalog on a skills event (TD-4502)", () => {
		startSlashCommands();
		expect(slashCommands.skills).toEqual([]);
		mocks.handler?.(skillsEvent([skill(), skill({ name: "ship", source: "user", fallback: true })]));
		expect(slashCommands.skills.map((s) => s.name)).toEqual(["review", "ship"]);
		mocks.handler?.(skillsEvent([]));
		expect(slashCommands.skills).toEqual([]);
	});

	it("ignores every other event", () => {
		startSlashCommands();
		mocks.handler?.(commandsEvent([command()]));
		mocks.handler?.(skillsEvent([skill()]));
		mocks.handler?.({ type: "policy_rules", seq: 2, rules: [] });
		expect(slashCommands.items).toHaveLength(1);
		expect(slashCommands.skills).toHaveLength(1);
	});
});

describe("requestCommands", () => {
	it("sends both listings keyed on the workspace — one menu, one ask", () => {
		requestCommands("/w");
		expect(mocks.sent).toEqual([
			{ type: "list_commands", workspace_path: "/w" },
			{ type: "list_skills", workspace_path: "/w" },
		]);
	});

	it("clears instead of asking when no workspace is open", () => {
		startSlashCommands();
		mocks.handler?.(commandsEvent([command()]));
		mocks.handler?.(skillsEvent([skill()]));
		requestCommands(null);
		expect(slashCommands.items).toEqual([]);
		expect(slashCommands.skills).toEqual([]);
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

describe("matchSkills", () => {
	const skills = [
		skill(),
		skill({ name: "Review-PR", source: "user" }),
		skill({ name: "ship", description: "cut a release" }),
	];

	it("filters case-insensitively on the prefix, like commands", () => {
		expect(matchSkills(skills, "rev").map((s) => s.name)).toEqual(["review", "Review-PR"]);
		expect(matchSkills(skills, "REV").map((s) => s.name)).toEqual(["review", "Review-PR"]);
		expect(matchSkills(skills, "sh").map((s) => s.name)).toEqual(["ship"]);
	});

	it("returns a copy of everything for an empty query", () => {
		const matched = matchSkills(skills, "");
		expect(matched).toHaveLength(3);
		expect(matched).not.toBe(skills);
	});
});
