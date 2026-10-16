// Screen pane store (TD-1710, TD-3401).
//
// Latest `screen_frame` path for the bound session. Preview bytes stay
// off the wire: the host reads a text data-URL sidecar under the session
// persist dir, same wall as artifacts. The tab appears after any
// computer-use tool (browser_* or desktop_*) this session.

import { onEvent } from "./connection-status.svelte.js";
import { isBridged } from "./bridged-id";
import { isTauri } from "./open-file";
import type { DaemonEventUnion, ToolCall } from "./protocol";
import { hostCommandError, readTextFileCommand } from "./host-read";
import { isCuTool, screenPreviewSidecar } from "./screen";

export const screen = $state({
	boundSessionId: null as string | null,
	hasFrame: false,
	hasCuTool: false,
	path: null as string | null,
	preview: null as string | null,
	error: null as string | null,
});

export type ScreenPreviewReader = (args: {
	path: string;
	sessionId: string;
}) => Promise<string>;

let sourceReader: ScreenPreviewReader = defaultScreenReader;
let started = false;
let previewToken = 0;
let stopEvents: (() => void) | null = null;
const toolNames = new Map<string, string>();

function ensureStarted(): void {
	if (started) return;
	started = true;
	stopEvents = onEvent(reduce);
}

export function setScreenPreviewReader(reader: ScreenPreviewReader | null): void {
	sourceReader = reader ?? defaultScreenReader;
}

export function startScreen(): () => void {
	ensureStarted();
	return () => {
		started = false;
		stopEvents?.();
		stopEvents = null;
	};
}

export function resetScreen(): void {
	started = false;
	stopEvents?.();
	stopEvents = null;
	previewToken += 1;
	toolNames.clear();
	screen.boundSessionId = null;
	screen.hasFrame = false;
	screen.hasCuTool = false;
	screen.path = null;
	screen.preview = null;
	screen.error = null;
	sourceReader = defaultScreenReader;
}

function reduce(event: DaemonEventUnion): void {
	// Another computer's screen frames name files on that computer; the
	// preview reads this one's disk (read_text_file), so it never follows them
	// (TD-5015).
	if ("session_id" in event && isBridged(event.session_id)) return;
	if (event.type === "tool_call") {
		rememberTool(event);
		return;
	}
	if (event.type === "tool_result") {
		const name = toolNames.get(event.tool_call_id);
		if (name !== undefined && isCuTool(name)) {
			screen.boundSessionId = event.session_id;
			screen.hasCuTool = true;
		}
		return;
	}
	if (event.type === "screen_frame") {
		if (!("path" in event) || typeof event.path !== "string") return;
		screen.boundSessionId = event.session_id;
		screen.hasFrame = true;
		screen.path = event.path;
		screen.error = null;
		void loadPreview(event.session_id, event.path);
	}
}

function rememberTool(event: ToolCall): void {
	toolNames.set(event.tool_call_id, event.name);
	if (isCuTool(event.name)) {
		screen.boundSessionId = event.session_id;
		screen.hasCuTool = true;
	}
}

async function loadPreview(sessionId: string, path: string): Promise<void> {
	const token = ++previewToken;
	try {
		const source = await sourceReader({
			path: screenPreviewSidecar(path),
			sessionId,
		});
		if (token !== previewToken) return;
		screen.preview = source;
	} catch (err) {
		if (token !== previewToken) return;
		screen.preview = null;
		screen.error = hostCommandError(err, "Could not read screen frame.");
	}
}

async function defaultScreenReader(args: { path: string; sessionId: string }): Promise<string> {
	if (!isTauri()) {
		throw new Error("Screen preview is available in the desktop app.");
	}
	const { invoke } = await import("@tauri-apps/api/core");
	return invoke<string>(
		"read_text_file",
		readTextFileCommand({
			path: args.path,
			workspace: null,
			sessionId: args.sessionId,
		}),
	);
}
