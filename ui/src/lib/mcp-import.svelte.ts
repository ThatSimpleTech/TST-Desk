// Paste a server's JSON, see what importing would do, then import (TD-5253).
//
// The daemon reads the text; this only holds it, shows the preview and
// collects the choices. The text can hold a token, so it is kept in memory
// only and cleared the moment an import succeeds. The preview never holds
// a value: the daemon does not send one.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, McpImportChoice, McpImportServer } from "./protocol";

export interface ImportRow {
	server: McpImportServer;
	/** Ticked to import. A refused server can never be ticked. */
	chosen: boolean;
	id: string;
	allowRemote: boolean;
	/** Values the preview asked for, by name. */
	inputs: Record<string, string>;
}

export const mcpImport = $state({
	text: "",
	/** The text the preview was made from; a later edit makes the preview stale. */
	previewedText: null as string | null,
	rows: [] as ImportRow[],
	error: null as string | null,
	busy: false,
	done: null as string | null,
});

const ID_RE = /^[a-z][a-z0-9-]{0,31}$/;
let started = false;
let importing = false;

export function startMcpImport(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetMcpImport(): void {
	mcpImport.text = "";
	mcpImport.previewedText = null;
	mcpImport.rows = [];
	mcpImport.error = null;
	mcpImport.busy = false;
	mcpImport.done = null;
	importing = false;
}

/** Why this row cannot be imported as it stands, or "" when it can. */
export function rowProblem(row: ImportRow, taken: readonly string[]): string {
	if (row.server.refused) return `Cannot be imported: ${row.server.refused}.`;
	if (!ID_RE.test(row.id)) return "The id must be lowercase letters, digits and -, starting with a letter.";
	if (taken.includes(row.id)) return "A server with this id is already listed. Give it another id.";
	if (row.server.off_box && !row.allowRemote) return "Tick the box to allow connecting to this server.";
	const missing = row.server.values.find(
		(value) => value.disposition === "needs_input" && !(row.inputs[value.name] ?? "").trim(),
	);
	if (missing) return `Type a value for ${missing.name}.`;
	return "";
}

/** Ids this import may not use: the ones listed already, and the other ticked rows'. */
export function takenIds(row: ImportRow, rows: readonly ImportRow[], listed: readonly string[]): string[] {
	return [...listed, ...rows.filter((other) => other !== row && other.chosen).map((other) => other.id)];
}

export function canImport(rows: readonly ImportRow[], listed: readonly string[], stale: boolean): boolean {
	const chosen = rows.filter((row) => row.chosen);
	if (stale || chosen.length === 0) return false;
	return chosen.every((row) => rowProblem(row, takenIds(row, rows, listed)) === "");
}

export function isStale(): boolean {
	return mcpImport.previewedText !== null && mcpImport.previewedText !== mcpImport.text;
}

export function previewImport(): void {
	const text = mcpImport.text;
	if (text.trim() === "") return;
	mcpImport.error = null;
	mcpImport.done = null;
	mcpImport.busy = true;
	importing = false;
	mcpImport.previewedText = text;
	if (!sendToDaemon({ type: "preview_mcp_import", text })) {
		mcpImport.busy = false;
		mcpImport.error = "Not connected.";
	}
}

export function runImport(listed: readonly string[]): void {
	if (mcpImport.previewedText === null || !canImport(mcpImport.rows, listed, isStale())) return;
	const servers: McpImportChoice[] = mcpImport.rows
		.filter((row) => row.chosen)
		.map((row) => ({
			source_name: row.server.source_name,
			id: row.id,
			...(row.allowRemote ? { allow_remote: true } : {}),
			...(Object.keys(row.inputs).length > 0 ? { inputs: { ...row.inputs } } : {}),
		}));
	mcpImport.error = null;
	mcpImport.busy = true;
	importing = true;
	if (!sendToDaemon({ type: "import_mcp_servers", text: mcpImport.previewedText, servers })) {
		mcpImport.busy = false;
		importing = false;
		mcpImport.error = "Not connected.";
	}
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "mcp_import_preview") {
		mcpImport.busy = false;
		mcpImport.error = event.error || null;
		mcpImport.rows = event.servers.map((server) => ({
			server,
			chosen: !server.refused,
			id: server.id,
			allowRemote: false,
			inputs: {},
		}));
		return;
	}
	if (event.type === "setup_state" && importing) {
		// The ack. The text may hold a token: drop it, and the typed values.
		const count = mcpImport.rows.filter((row) => row.chosen).length;
		resetMcpImport();
		mcpImport.done = count === 1 ? "Imported 1 server." : `Imported ${count} servers.`;
		return;
	}
	if (
		event.type === "error" &&
		(event.in_reply_to === "import_mcp_servers" || event.in_reply_to === "preview_mcp_import")
	) {
		mcpImport.busy = false;
		importing = false;
		mcpImport.error = event.message;
	}
}
