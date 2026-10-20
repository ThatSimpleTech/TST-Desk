// @vitest-environment jsdom
//
// TD-5110: Settings → MCP servers puts a server's token in the keychain
// without ever showing it. The value field is a password input, it is
// cleared once sent, and the store and the page keep only names.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import SettingsMcp from "./components/SettingsMcp.svelte";
import { settings } from "./settings.svelte.js";

const TOKEN = "td5110-ui-Wq7zP3kLm9";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function input(placeholder: string): HTMLInputElement {
	const el = target.querySelector(`input[placeholder="${placeholder}"]`);
	if (!(el instanceof HTMLInputElement)) throw new Error(`no input ${placeholder}`);
	return el;
}

function type(el: HTMLInputElement, value: string): void {
	el.value = value;
	el.dispatchEvent(new Event("input", { bubbles: true }));
	flushSync();
}

function button(name: string): HTMLButtonElement {
	const found = [...target.querySelectorAll("button")].find(
		(b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === name,
	);
	if (!found) throw new Error(`no button ${name}`);
	return found;
}

beforeEach(() => {
	mocks.sent = [];
	settings.mcpServers = [
		{
			id: "gh",
			transport: "stdio",
			command: ["npx", "gh-mcp"],
			url: "",
			enabled: true,
			values: [
				{ name: "GITHUB_TOKEN", source: "keychain", stored: true },
				{ name: "LOG_LEVEL", source: "literal", stored: false },
			],
		},
		{ id: "local", transport: "http", command: [], url: "http://127.0.0.1:9/mcp", enabled: true },
	];
	target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(SettingsMcp, { target });
	flushSync();
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
	settings.mcpServers = [];
});

describe("SettingsMcp keychain values", () => {
	it("lists names and where each lives, never a value", () => {
		const text = target.textContent ?? "";
		expect(text).toContain("GITHUB_TOKEN · in the keychain");
		expect(text).toContain("LOG_LEVEL · set in config.yaml");
	});

	it("sends the value once, from a password field it then clears", () => {
		const value = input("Paste token");
		expect(value.type).toBe("password");
		expect(button("Save to keychain").disabled).toBe(true);
		type(input("GITHUB_TOKEN"), "API_TOKEN");
		type(value, `  ${TOKEN} `);
		const save = target.querySelectorAll<HTMLButtonElement>("button");
		const first = [...save].find((b) => b.textContent?.trim() === "Save to keychain");
		first?.click();
		flushSync();
		expect(mocks.sent).toEqual([
			{ type: "set_mcp_secret", server_id: "gh", target: "env", name: "API_TOKEN", value: TOKEN },
		]);
		expect(input("Paste token").value).toBe("");
		expect(JSON.stringify(settings)).not.toContain(TOKEN);
		expect(target.innerHTML).not.toContain(TOKEN);
	});

	it("names a header for an http server", () => {
		type(input("Authorization"), "Authorization");
		type(input("Bearer …"), `Bearer ${TOKEN}`);
		const saves = [...target.querySelectorAll<HTMLButtonElement>("button")].filter(
			(b) => b.textContent?.trim() === "Save to keychain",
		);
		saves[1].click();
		expect(mocks.sent).toEqual([
			{
				type: "set_mcp_secret",
				server_id: "local",
				target: "header",
				name: "Authorization",
				value: `Bearer ${TOKEN}`,
			},
		]);
	});

	it("removes one value by name", () => {
		button("Remove GITHUB_TOKEN from gh").click();
		expect(mocks.sent).toEqual([
			{ type: "delete_mcp_secret", server_id: "gh", target: "env", name: "GITHUB_TOKEN" },
		]);
	});
});
