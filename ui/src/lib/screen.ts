// Screen pane helpers (TD-1710, TD-3401).
//
// Pure: which tools count as computer-use, when the tab appears,
// and the empty copy that points at the first computer-use turn.

export const BROWSER_TOOLS = new Set([
	"browser_navigate",
	"browser_click",
	"browser_type",
	"browser_scroll",
	"browser_screenshot",
	"browser_wait",
]);

export const DESKTOP_TOOLS = new Set([
	"desktop_screenshot",
	"desktop_move",
	"desktop_click",
	"desktop_type",
	"desktop_scroll",
]);

/** Empty-state copy. The Screen fills on the first computer-use turn. */
export const SCREEN_EMPTY_COPY =
	"No frame yet. The Screen fills on the first computer-use turn.";

export function isBrowserTool(name: string): boolean {
	return BROWSER_TOOLS.has(name);
}

export function isCuTool(name: string): boolean {
	return BROWSER_TOOLS.has(name) || DESKTOP_TOOLS.has(name);
}

/** Capture cannot actuate — Design may stay on during a screenshot. */
const CU_CAPTURE = new Set(["browser_screenshot", "desktop_screenshot"]);

export function isActuatingCuTool(name: string): boolean {
	return isCuTool(name) && !CU_CAPTURE.has(name);
}

export function screenTabVisible(args: {
	boundSessionId: string | null;
	sessionId: string | null;
	hasFrame: boolean;
	hasCuTool: boolean;
}): boolean {
	if (args.sessionId === null || args.boundSessionId !== args.sessionId) return false;
	return args.hasFrame || args.hasCuTool;
}

/** Sidecar the host can read as text (same wall as artifacts). */
export function screenPreviewSidecar(path: string): string {
	return path.replace(/\.png$/i, ".dataurl");
}
