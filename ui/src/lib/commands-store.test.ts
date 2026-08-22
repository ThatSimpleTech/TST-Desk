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

import type { CommandSummary } from "./protocol";
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

beforeEach(() => {
	resetCommandMenu();
	mocks.sent.length = 0;
	mocks.eventHandlers.clear();
	startCommandMenu();
});

describe("fetching", () => {
	it("asks the daemon once per session", () => {
		ensureCommands("sess-1");
		ensureCommands("sess-1");
		expect(mocks.sent).toEqual([{ type: "list_commands", session_id: "sess-1" }]);
	});

	it("re-asks when the session changes", () => {
		ensureCommands("sess-1");
		emit(commandsList([command("a")]));
		ensureCommands("sess-2");
		expect(mocks.sent).toHaveLength(2);
	});

	it("stores the reply and clears the pending mark", () => {
		ensureCommands("sess-1");
		emit(commandsList([command("deploy"), command("review")]));
		expect(commandMenu.commands.map((c) => c.name)).toEqual(["deploy", "review"]);
		// The listing landed, so a later keystroke's ensure is a no-op.
		ensureCommands("sess-1");
		expect(mocks.sent).toHaveLength(1);
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
