// Open a session in a second window (TD-4703).

export const session_window = {
	bindQuery: "bind_session",
} as const;

/** Ask the host to open (or focus) a viewer for *sessionId*. */
export async function openSessionWindow(sessionId: string): Promise<boolean> {
	const cleaned = sessionId.trim();
	if (cleaned === "") return false;
	try {
		const { invoke } = await import("@tauri-apps/api/core");
		await invoke("open_session_window", { sessionId: cleaned });
		return true;
	} catch {
		return false;
	}
}
