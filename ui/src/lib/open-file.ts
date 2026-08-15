// Open resolved steering files in the system editor (TD-1201).
//
// Paths come from the daemon's assembled stack — never from free-text
// input — so handing them to the OS default application is safe. Outside
// the Tauri shell (browser dev, tests) opening is a no-op; the panel
// still reads.

/** True when running inside the Tauri shell. */
export function isTauri(): boolean {
	return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Open *path* with the OS default application (tauri-plugin-opener).
 *  Best-effort: returns false when there is no shell to ask or the open
 *  failed, so the caller can decide whether to surface anything. */
export async function openInEditor(path: string): Promise<boolean> {
	if (!isTauri()) return false;
	try {
		const { openPath } = await import("@tauri-apps/plugin-opener");
		await openPath(path);
		return true;
	} catch {
		return false;
	}
}
