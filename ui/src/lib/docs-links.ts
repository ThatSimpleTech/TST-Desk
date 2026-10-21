// Links to the user guides in docs/ (TD-5172). They open in the system
// browser on the project's public repository, so a new user reads the same
// page the repo ships.

import { isTauri } from "./open-file";

export const DOCS_BASE = "https://github.com/ThatSimpleTech/TST-Desk/blob/main/docs";

/** "How models work": presets, labels, teams, and the Add a model steps. */
export const MODELS_DOC_URL = `${DOCS_BASE}/models.md`;

/** Open *url* in the system browser. Outside the desktop shell (tests, a
 *  plain browser) it falls back to a new tab. */
export async function openDoc(url: string): Promise<void> {
	if (!isTauri()) {
		if (typeof window !== "undefined") window.open(url, "_blank", "noopener");
		return;
	}
	const { openUrl } = await import("@tauri-apps/plugin-opener");
	await openUrl(url);
}
