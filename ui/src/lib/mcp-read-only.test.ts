// @vitest-environment jsdom
//
// TD-5276, TD-5279: an MCP stdio server is a command the computer runs. Off
// the computer the page is the list and nothing else, and it sends nothing,
// unless the computer switched "MCP servers" on for phones.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import SettingsMcp from "./components/SettingsMcp.svelte";
import { settings, type McpServerRow } from "./settings.svelte.js";

let component: ReturnType<typeof mount> | null = null;

const SERVERS = [
	{
		id: "files",
		transport: "stdio",
		command: ["npx", "files-server"],
		url: "",
		enabled: true,
		excludedTools: [],
		values: [{ name: "FILES_TOKEN", stored: true, literal: false }],
	},
	{
		id: "docs",
		transport: "http",
		command: [],
		url: "http://127.0.0.1:9000/mcp",
		enabled: false,
		excludedTools: [],
		values: [],
	},
] as unknown as McpServerRow[];

function open(canManage: boolean): HTMLElement {
	settings.mcpServers = SERVERS;
	const target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(SettingsMcp, { target, props: { canManage } });
	flushSync();
	return target;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
	settings.mcpServers = [];
});

describe("Settings → MCP servers, off the computer", () => {
	it("shows each server, what it runs and whether it is on", () => {
		const root = open(false);
		const text = root.textContent ?? "";
		expect(text).toContain("files");
		expect(text).toContain("npx files-server");
		expect(text).toContain("FILES_TOKEN");
		expect(text).toContain("http://127.0.0.1:9000/mcp");
		const states = [...root.querySelectorAll(".row .state")].map((el) => el.textContent?.trim());
		expect(states).toEqual(["On", "Off"]);
	});

	it("has nothing to press or type into, and says where to make changes", () => {
		const root = open(false);
		expect(root.querySelectorAll("button")).toHaveLength(0);
		expect(root.querySelectorAll("input")).toHaveLength(0);
		expect(root.textContent).toContain(
			"Adding or changing MCP servers from a phone is turned off. You can turn it on under Settings, Phones and devices on the computer.",
		);
		expect(mocks.sent).toEqual([]);
	});
});

describe("Settings → MCP servers, on the computer", () => {
	it("still has the switch, the value field, Remove and Add server", () => {
		const root = open(true);
		const names = [...root.querySelectorAll("button")].map(
			(b) => b.getAttribute("aria-label") ?? b.textContent?.trim() ?? "",
		);
		expect(names).toContain("Add server");
		expect(names).toContain("Remove FILES_TOKEN from files");
		// One per server; the Add form's argument row has a Remove of its own.
		expect(root.querySelectorAll(".row .actions .btn--danger")).toHaveLength(2);
		expect(root.querySelectorAll('[role="switch"]')).toHaveLength(2);
		expect(root.textContent).not.toContain("on the computer.");
	});

	it("is what the page does when it is told nothing", () => {
		settings.mcpServers = SERVERS;
		const target = document.createElement("div");
		document.body.appendChild(target);
		component = mount(SettingsMcp, { target });
		flushSync();
		expect(target.querySelectorAll('[role="switch"]')).toHaveLength(2);
	});
});
