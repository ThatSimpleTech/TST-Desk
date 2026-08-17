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
	/** The command palette is open (TD-1707) — a modal layer of its own. */
	paletteOpen: boolean;
	/** A modal pane (wizard, doctor, decisions, settings, palette) is open. */
	modalOpen: boolean;
	/** A turn is running or parked awaiting approval (chat-store showCancel). */
	turnLive: boolean;
}

export type ShortcutAction =
	| "close-menu"
	| "close-palette"
	| "cancel-turn"
	| "open-settings"
	| "open-palette";

/** Map a keydown to one app-level action, or null when nothing applies. */
export function resolveShortcut(
	event: ShortcutEvent,
	context: ShortcutContext,
): ShortcutAction | null {
	if (event.key === "Escape" && !event.metaKey && !event.ctrlKey) {
		if (context.workspaceMenuOpen) return "close-menu";
		// The palette dismisses itself (TD-1707) — it is a modal layer, so it
		// eats the key rather than letting it reach the turn behind it.
		if (context.paletteOpen) return "close-palette";
		// Other modals keep today's close-button behavior; Escape never reaches
		// through one to cancel the turn behind it.
		if (context.modalOpen) return null;
		return context.turnLive ? "cancel-turn" : null;
	}
	// ⌘, on macOS, Ctrl+, elsewhere — opens settings (TD-1703). It used to
	// reopen the wizard; the wizard is first-run only now, so the key that
	// every app spends on preferences points at preferences.
	if (event.key === "," && (event.metaKey || event.ctrlKey)) return "open-settings";
	// ⌘K / Ctrl+K — the command palette (TD-1707). Works from anywhere,
	// including on top of an open modal; opening it twice just clears it.
	if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey))
		return "open-palette";
	return null;
}
