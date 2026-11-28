// Open resolved steering files in the system editor (TD-1201).
//
// Paths come from the daemon's assembled stack — never from free-text
// input — so handing them to the OS default application is safe. Outside
// the Tauri shell (browser dev, tests) opening is a no-op; the panel
// still reads.

import { isBridged } from "./bridged-id";

/** True when running inside the Tauri shell. */
export function isTauri(): boolean {
	return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Open *path* with the OS default application, through the host's
 *  `open_path` command, which checks the path first (PKG-12). The window has
 *  no open-path grant of its own: the plugin's was granted with no scope,
 *  which allows nothing, so every file click did nothing in a release build.
 *  A refusal or a failed open says so; false when nothing opened. */
export async function openInEditor(path: string): Promise<boolean> {
	if (!isTauri()) return false;
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		await invoke("open_path", { path });
		return true;
	} catch (error) {
		const { notify } = await import("./notifications.svelte.js");
		notify(`open-path:${path}`, {
			severity: "toast",
			title: "Couldn't open the file",
			body: typeof error === "string" && error !== "" ? error : `Could not open ${path}.`,
		});
		return false;
	}
}

/** Open a path a session's events named (TD-5015). Never for another
 *  computer's session: the path is on that computer, and under a self-pair
 *  it would even open here, so the gate is the id, not whether it exists. */
export async function openSessionPath(sessionId: string | null, path: string): Promise<boolean> {
	if (isBridged(sessionId)) return false;
	return openInEditor(path);
}
