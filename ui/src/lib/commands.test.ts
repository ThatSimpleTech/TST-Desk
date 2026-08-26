// Slash-command store (TD-4501). The daemon is the source of names.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

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

import { commands, loadCommands, resetCommands, startCommands } from "./commands.svelte.js";
import { filterCommands, insertCommandBody, slashQuery } from "./slash-commands";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

beforeEach(() => {
	mocks.sent.length = 0;
	resetCommands();
	startCommands();
	mocks.sent.length = 0;
});

afterEach(() => {
	resetCommands();
});

describe("store", () => {
	it("asks the daemon for the list and inserts a picked body", () => {
		loadCommands("/ws");
		expect(mocks.sent).toEqual([{ type: "list_commands", workspace_path: "/ws" }]);
		emit({
			type: "command_list",
			seq: 1,
			workspace_path: "/ws",
			commands: [
				{
					name: "review",
					description: "Review the diff",
					source: "workspace",
					body: "Please review.\n",
				},
			],
		});
		expect(commands.items.map((c) => c.name)).toEqual(["review"]);
		expect(slashQuery("/re")).toBe("re");
		const filtered = filterCommands(commands.items, "re");
		expect(filtered).toHaveLength(1);
		expect(insertCommandBody("/re", filtered[0]!.body)).toBe("Please review.\n");
	});

	it("ignores a list for a different workspace", () => {
		loadCommands("/ws");
		emit({
			type: "command_list",
			seq: 1,
			workspace_path: "/other",
			commands: [
				{
					name: "review",
					description: "",
					source: "workspace",
					body: "nope\n",
				},
			],
		});
		expect(commands.items).toEqual([]);
	});
});
