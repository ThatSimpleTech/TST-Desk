// Slash-command store tests (TD-4501).
//
// Mocks only the connection seam (the palette-store.test.ts posture) and
// asserts on the real store's observable state: one fetch per session,
// the commands_list reducer, and the Escape-dismiss contract that
// shortcuts.ts drives.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => {
	const eventHandlers = new Set<(e: DaemonEventUnion) => void>();
	return {
		eventHandlers,
		sent: [] as ClientMessageUnion[],
	};
});

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.eventHandlers.add(handler);
		return () => {
			mocks.eventHandlers.delete(handler);
		};
	},
	onConnectionState: () => () => {},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import type { CommandSummary, SkillSummary } from "./protocol";
import {
	commandMenu,
	dismissSlashMenu,
	ensureCommands,
	moveSelection,
	resetCommandMenu,
	resetSelection,
	setSlashState,
	startCommandMenu,
} from "./commands-store.svelte.js";

function emit(event: DaemonEventUnion): void {
	for (const handler of mocks.eventHandlers) handler(event);
}

function command(name: string): CommandSummary {
	return { name, source: "workspace", description: null, body: `${name}\n`, line_count: 1 };
}

function commandsList(commands: CommandSummary[]): DaemonEventUnion {
	return { type: "commands_list", seq: 1, commands } as DaemonEventUnion;
}

function skill(name: string): SkillSummary {
	return { name, source: "user", description: `${name} does things`, when_to_use: null, line_count: 1 };
}

function skillsList(skills: SkillSummary[]): DaemonEventUnion {
	return { type: "skills_list", seq: 1, skills } as DaemonEventUnion;
}

beforeEach(() => {
	resetCommandMenu();
	mocks.sent.length = 0;
	mocks.eventHandlers.clear();
	startCommandMenu();
});

describe("fetching", () => {
	it("asks for both lists once per session (TD-4502)", () => {
		ensureCommands("sess-1");
		ensureCommands("sess-1");
		expect(mocks.sent).toEqual([
			{ type: "list_commands", session_id: "sess-1" },
			{ type: "list_skills", session_id: "sess-1" },
		]);
	});

	it("re-asks when the session changes", () => {
		ensureCommands("sess-1");
		emit(commandsList([command("a")]));
		emit(skillsList([skill("b")]));
		ensureCommands("sess-2");
		expect(mocks.sent).toHaveLength(4);
	});

	it("stores the reply and clears the pending mark", () => {
		ensureCommands("sess-1");
		emit(commandsList([command("deploy"), command("review")]));
		expect(commandMenu.commands.map((c) => c.name)).toEqual(["deploy", "review"]);
		// The listing landed, so a later keystroke's ensure is a no-op.
		ensureCommands("sess-1");
		expect(mocks.sent).toHaveLength(2);
	});
});

describe("the skills listing (TD-4502)", () => {
	it("stores skills_list without un-marking the fetched session", () => {
		ensureCommands("sess-1");
		emit(commandsList([command("deploy")]));
		emit(skillsList([skill("triage")]));
		expect(commandMenu.skills.map((s) => s.name)).toEqual(["triage"]);
		// Either reply completes the pair; the second must not clear the
		// mark again or every keystroke refetches.
		ensureCommands("sess-1");
		expect(mocks.sent).toHaveLength(2);
	});

	it("skills_list alone still completes the fetch", () => {
		ensureCommands("sess-1");
		emit(skillsList([skill("triage")]));
		ensureCommands("sess-1");
		expect(mocks.sent).toHaveLength(2);
	});
});

describe("the Escape contract (shortcuts.ts drives dismissal)", () => {
	beforeEach(() => {
		emit(commandsList([command("deploy")]));
	});

	it("dismiss only lands while open, pinning the live query", () => {
		dismissSlashMenu(); // closed: nothing pinned
		expect(commandMenu.dismissedQuery).toBeNull();

		setSlashState(true, "de");
		dismissSlashMenu();
		expect(commandMenu.dismissedQuery).toBe("de");
		expect(commandMenu.selected).toBe(0);
	});

	it("the pin is exact, so typing past it reopens", () => {
		setSlashState(true, "");
		dismissSlashMenu();
		expect(commandMenu.dismissedQuery).toBe("");
		// Composer shows the menu when slashQuery(draft) !== this pin: bare
		// "/" stays dismissed, "/d" no longer matches and reopens.
	});
});

describe("selection", () => {
	it("wraps both ways and resets to the top", () => {
		emit(commandsList([command("a"), command("b"), command("c")]));
		moveSelection(-1, 3);
		expect(commandMenu.selected).toBe(2);
		moveSelection(1, 3);
		expect(commandMenu.selected).toBe(0);
		emit(commandsList([command("x")]));
		expect(commandMenu.selected).toBe(0);
		resetSelection();
		expect(commandMenu.selected).toBe(0);
	});
});
