// Per-window session binding (TD-4703).
//
// Secondary windows open with `?bind_session=<id>` and must stay on that
// session instead of auto-adopting the newest live one.

import { session_window } from "./open-session-window";

/** When set, this webview follows one session only. */
export function windowBindSessionId(): string | null {
	if (typeof window === "undefined") return null;
	const params = new URLSearchParams(window.location.search);
	const fromQuery = params.get(session_window.bindQuery);
	if (fromQuery !== null && fromQuery.trim() !== "") {
		return fromQuery.trim();
	}
	return null;
}

/** True when this webview is a dedicated session viewer, not the main shell. */
export function isSessionViewerWindow(): boolean {
	return windowBindSessionId() !== null;
}
