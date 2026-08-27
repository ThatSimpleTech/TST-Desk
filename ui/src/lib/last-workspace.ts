// Last-workspace persistence for quick entry (TD-4702).
//
// The host owns the file; the UI only writes when the main window opens or
// focuses a workspace. Best-effort — a failed invoke must not block chat.

import { isTauri } from "./open-file";

/** Remember *path* as the workspace quick entry should target. */
export async function persistLastWorkspace(path: string): Promise<void> {
	if (!isTauri()) return;
	const cleaned = path.trim();
	if (cleaned === "") return;
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		await invoke("set_last_workspace", { path: cleaned });
	} catch {
		// Host unavailable or old binary — quick entry falls back to recents.
	}
}

/** Read the host's last workspace path, or null when unset / unavailable. */
export async function readLastWorkspace(): Promise<string | null> {
	if (!isTauri()) return null;
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		const path = await invoke<string | null>("get_last_workspace");
		if (typeof path !== "string") return null;
		const cleaned = path.trim();
		return cleaned === "" ? null : cleaned;
	} catch {
		return null;
	}
}

/** Hide the quick-entry overlay after a send or Esc. */
export async function hideQuickEntryOverlay(): Promise<void> {
	if (!isTauri()) return;
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		await invoke("hide_quick_entry");
	} catch {
		// Nothing to do outside the shell.
	}
}
