// @vitest-environment jsdom
//
// TD-5253: paste a server's JSON, see what would run, then import. The
// daemon reads the text and decides; these pin what the window sends, when
// Import is allowed, and that the pasted text is dropped once it is used.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion, McpImportServer } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
	listeners: new Set<(event: DaemonEventUnion) => void>(),
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (fn: (event: DaemonEventUnion) => void) => {
		mocks.listeners.add(fn);
		return () => mocks.listeners.delete(fn);
	},
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import SettingsMcpImport from "./components/SettingsMcpImport.svelte";
import { settings, type McpServerRow } from "./settings.svelte.js";
import {
	canImport,
	mcpImport,
	resetMcpImport,
	rowProblem,
	type ImportRow,
} from "./mcp-import.svelte.js";

let component: ReturnType<typeof mount> | null = null;

function emit(event: unknown): void {
	for (const fn of [...mocks.listeners]) fn(event as DaemonEventUnion);
	flushSync();
}

function server(over: Partial<McpImportServer> = {}): McpImportServer {
	return {
		source_name: "GitHub",
		id: "github",
		transport: "stdio",
		command: ["npx", "-y", "server-github"],
		url: "",
		off_box: false,
		exists: false,
		values: [{ name: "GITHUB_TOKEN", target: "env", disposition: "keychain" }],
		not_imported: [],
		notes: [],
		refused: "",
		...over,
	};
}

function row(over: Partial<McpImportServer> = {}, more: Partial<ImportRow> = {}): ImportRow {
	const s = server(over);
	return { server: s, chosen: true, id: s.id, allowRemote: false, inputs: {}, ...more };
}

function show(): HTMLElement {
	const target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(SettingsMcpImport, { target });
	flushSync();
	return target;
}

function press(root: ParentNode, label: string): void {
	const button = [...root.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
	if (!button) throw new Error(`no button "${label}"`);
	button.click();
	flushSync();
}

function type(root: ParentNode, label: string, value: string): void {
	const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`);
	if (!field) throw new Error(`no field "${label}"`);
	field.value = value;
	field.dispatchEvent(new Event("input", { bubbles: true }));
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
	settings.mcpServers = [];
	resetMcpImport();
});

const TEXT = '{"mcpServers": {"GitHub": {"command": "npx"}}}';

describe("when Import is allowed", () => {
	it("needs a valid id that is not taken", () => {
		expect(rowProblem(row(), [])).toBe("");
		expect(rowProblem(row({}, { id: "Not A Slug" }), [])).toContain("lowercase");
		expect(rowProblem(row(), ["github"])).toContain("already listed");
	});

	it("needs the tick for a server on another machine", () => {
		const remote = { transport: "http" as const, url: "https://mcp.example.test/x", off_box: true };
		expect(rowProblem(row(remote), [])).toContain("Tick the box");
		expect(rowProblem(row(remote, { allowRemote: true }), [])).toBe("");
	});

	it("needs every asked-for value typed", () => {
		const asks = {
			values: [{ name: "API_KEY", target: "env" as const, disposition: "needs_input" as const }],
		};
		expect(rowProblem(row(asks), [])).toBe("Type a value for API_KEY.");
		expect(rowProblem(row(asks, { inputs: { API_KEY: "   " } }), [])).toBe("Type a value for API_KEY.");
		expect(rowProblem(row(asks, { inputs: { API_KEY: "typed-value" } }), [])).toBe("");
	});

	it("never for a refused server, a stale preview, or nothing ticked", () => {
		expect(rowProblem(row({ refused: "it uses SSE" }), [])).toContain("Cannot be imported");
		expect(canImport([row()], [], false)).toBe(true);
		expect(canImport([row()], [], true)).toBe(false);
		expect(canImport([row({}, { chosen: false })], [], false)).toBe(false);
		// One bad ticked row holds back the lot; an unticked bad row does not.
		expect(canImport([row(), row({ source_name: "B", id: "github" })], [], false)).toBe(false);
		expect(canImport([row(), row({ refused: "no" }, { chosen: false })], [], false)).toBe(true);
	});
});

describe("Settings → MCP servers, Paste JSON", () => {
	it("previews, shows what would run, and sends nothing else until Import", () => {
		const root = show();
		press(root, "Paste JSON");
		type(root, "MCP server JSON", TEXT);
		press(root, "Preview");
		expect(mocks.sent).toEqual([{ type: "preview_mcp_import", text: TEXT }]);
		emit({ type: "mcp_import_preview", seq: 1, servers: [server()], error: "" });
		expect(root.textContent).toContain("npx -y server-github");
		expect((root.textContent ?? "").replace(/\s+/g, " ")).toContain(
			"GITHUB_TOKEN · goes to the keychain",
		);
		expect(mocks.sent).toHaveLength(1);
		press(root, "Import");
		expect(mocks.sent[1]).toEqual({
			type: "import_mcp_servers",
			text: TEXT,
			servers: [{ source_name: "GitHub", id: "github" }],
		});
	});

	it("drops the pasted text and the typed values once the import is acknowledged", () => {
		const root = show();
		press(root, "Paste JSON");
		type(root, "MCP server JSON", TEXT);
		press(root, "Preview");
		emit({
			type: "mcp_import_preview",
			seq: 1,
			error: "",
			servers: [
				server({ values: [{ name: "API_KEY", target: "env", disposition: "needs_input", reason: "Type it." }] }),
			],
		});
		const importButton = [...root.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Import");
		expect(importButton?.disabled).toBe(true);
		type(root, "Value for API_KEY", "typed-value-0001");
		press(root, "Import");
		expect(mocks.sent[1]).toMatchObject({ servers: [{ inputs: { API_KEY: "typed-value-0001" } }] });
		emit({ type: "setup_state", seq: 1, has_api_key: false, active_preset: "" });
		expect(mcpImport.text).toBe("");
		expect(mcpImport.rows).toEqual([]);
		expect(mcpImport.previewedText).toBeNull();
		expect(root.textContent).toContain("Imported 1 server.");
		expect(root.innerHTML).not.toContain("typed-value-0001");
	});

	it("a setup_state that is not the ack leaves the text alone", () => {
		const root = show();
		press(root, "Paste JSON");
		type(root, "MCP server JSON", TEXT);
		emit({ type: "setup_state", seq: 1, has_api_key: false, active_preset: "" });
		expect(mcpImport.text).toBe(TEXT);
	});

	it("says why a server cannot be imported, and it cannot be ticked", () => {
		const root = show();
		press(root, "Paste JSON");
		type(root, "MCP server JSON", TEXT);
		press(root, "Preview");
		emit({
			type: "mcp_import_preview",
			seq: 1,
			error: "",
			servers: [server({ refused: "it needs an OAuth sign-in" })],
		});
		expect(root.textContent).toContain("Cannot be imported: it needs an OAuth sign-in.");
		expect(root.querySelector<HTMLInputElement>('[aria-label="Import GitHub"]')?.disabled).toBe(true);
		const importButton = [...root.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Import");
		expect(importButton?.disabled).toBe(true);
	});

	it("makes a changed text be previewed again, and shows the daemon's reason on a refusal", () => {
		settings.mcpServers = [{ id: "github" } as McpServerRow];
		const root = show();
		press(root, "Paste JSON");
		type(root, "MCP server JSON", TEXT);
		press(root, "Preview");
		emit({ type: "mcp_import_preview", seq: 1, servers: [server()], error: "" });
		expect(root.textContent).toContain("already listed");
		type(root, "Id for GitHub", "github-2");
		type(root, "MCP server JSON", TEXT + " ");
		expect(root.textContent).toContain("Preview it again");
		const importButton = [...root.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Import");
		expect(importButton?.disabled).toBe(true);
		emit({ type: "mcp_import_preview", seq: 1, servers: [], error: "That is not JSON (line 1, column 2)." });
		expect(root.textContent).toContain("That is not JSON (line 1, column 2).");
		emit({ type: "error", code: "bad_request", message: "'GitHub': type a value for X.", in_reply_to: "import_mcp_servers" });
		expect(root.textContent).toContain("'GitHub': type a value for X.");
	});
});
