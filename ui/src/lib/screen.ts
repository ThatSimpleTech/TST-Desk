// Screen pane helpers (TD-1710).
//
// Pure: which tools count as browser computer-use, when the tab appears,
// and the empty copy that points at the first computer-use turn.

export const BROWSER_TOOLS = new Set([
	"browser_navigate",
	"browser_click",
	"browser_type",
	"browser_scroll",
	"browser_screenshot",
	"browser_wait",
]);

/** Empty-state copy. The Screen starts on the first computer-use turn. */
export const SCREEN_EMPTY_COPY =
	"No browser frame yet. The Screen starts when the first computer-use turn captures a page.";

export function isBrowserTool(name: string): boolean {
	return BROWSER_TOOLS.has(name);
}

export function screenTabVisible(args: {
	boundSessionId: string | null;
	sessionId: string | null;
	hasFrame: boolean;
	hasBrowserTool: boolean;
}): boolean {
	if (args.sessionId === null || args.boundSessionId !== args.sessionId) return false;
	return args.hasFrame || args.hasBrowserTool;
}

/** Sidecar the host can read as text (same wall as artifacts). */
export function screenPreviewSidecar(path: string): string {
	return path.replace(/\.png$/i, ".dataurl");
}
