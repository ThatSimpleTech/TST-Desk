// Global keyboard shortcuts (TD-1609).
//
// Pure and rune-free so the mapping is unit-testable in the node vitest
// environment; AppShell wires it to the stores via <svelte:window>. Escape
// peels layers from the top down — an open menu or modal eats it before it
// can cancel a turn — so one key never has two visible effects at once.

export interface ShortcutEvent {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
}

export interface ShortcutContext {
	/** The title-bar workspace recents menu is open. */
	workspaceMenuOpen: boolean;
	/** A modal pane (wizard, doctor, decisions) is open. */
	modalOpen: boolean;
	/** A turn is running or parked awaiting approval (chat-store showCancel). */
	turnLive: boolean;
}

export type ShortcutAction = "close-menu" | "cancel-turn" | "open-wizard";

/** Map a keydown to one app-level action, or null when nothing applies. */
export function resolveShortcut(
	event: ShortcutEvent,
	context: ShortcutContext,
): ShortcutAction | null {
	if (event.key === "Escape" && !event.metaKey && !event.ctrlKey) {
		if (context.workspaceMenuOpen) return "close-menu";
		// Modals keep today's close-button behavior; Escape never reaches
		// through one to cancel the turn behind it.
		if (context.modalOpen) return null;
		return context.turnLive ? "cancel-turn" : null;
	}
	// ⌘, on macOS, Ctrl+, elsewhere — reopens the setup wizard from anywhere.
	if (event.key === "," && (event.metaKey || event.ctrlKey)) return "open-wizard";
	return null;
}
