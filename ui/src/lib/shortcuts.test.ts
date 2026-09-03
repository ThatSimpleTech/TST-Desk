// Shortcut mapping tests (TD-1609): Escape layer order, the ⌘, settings
// shortcut and the ⌘K palette (TD-1707), including the combos that must
// stay inert.

import { describe, expect, it } from "vitest";
import { resolveShortcut, sessionSlot, type ShortcutContext } from "./shortcuts";

const IDLE: ShortcutContext = {
	workspaceMenuOpen: false,
	paletteOpen: false,
	modalOpen: false,
	turnLive: false,
};
const LIVE: ShortcutContext = { ...IDLE, turnLive: true };

function esc(metaKey = false, ctrlKey = false) {
	return { key: "Escape", metaKey, ctrlKey };
}

describe("resolveShortcut — Escape", () => {
	it("cancels the running turn when nothing is layered above it", () => {
		expect(resolveShortcut(esc(), LIVE)).toBe("cancel-turn");
	});

	it("closes the workspace menu before touching the turn", () => {
		expect(resolveShortcut(esc(), { ...LIVE, workspaceMenuOpen: true })).toBe("close-menu");
	});

	it("closes the top modal rather than the turn behind it (TD-1013)", () => {
		expect(resolveShortcut(esc(), { ...LIVE, modalOpen: true })).toBe("close-modal");
		expect(resolveShortcut(esc(), { ...IDLE, modalOpen: true })).toBe("close-modal");
	});

	it("dismisses the palette without reaching the turn behind it (TD-1707)", () => {
		// The palette counts in modalOpen too, so this is the layered case: it
		// must close the palette, and it must not cancel the running turn.
		expect(resolveShortcut(esc(), { ...LIVE, paletteOpen: true, modalOpen: true })).toBe(
			"close-palette",
		);
	});

	it("closes the menu before the palette, and the palette before a pane", () => {
		expect(
			resolveShortcut(esc(), { ...LIVE, workspaceMenuOpen: true, paletteOpen: true }),
		).toBe("close-menu");
		expect(resolveShortcut(esc(), { ...IDLE, paletteOpen: true, modalOpen: true })).toBe(
			"close-palette",
		);
	});

	it("does nothing when the session is idle", () => {
		expect(resolveShortcut(esc(), IDLE)).toBeNull();
	});

	it("ignores modified Escapes so browser/system combos pass through", () => {
		expect(resolveShortcut(esc(true), LIVE)).toBeNull();
		expect(resolveShortcut(esc(false, true), LIVE)).toBeNull();
	});
});

describe("resolveShortcut — settings (TD-1703)", () => {
	it("opens on ⌘, (meta) and Ctrl+,", () => {
		expect(resolveShortcut({ key: ",", metaKey: true, ctrlKey: false }, IDLE)).toBe(
			"open-settings",
		);
		expect(resolveShortcut({ key: ",", metaKey: false, ctrlKey: true }, IDLE)).toBe(
			"open-settings",
		);
	});

	it("works while a turn runs and while a modal is already open", () => {
		expect(resolveShortcut({ key: ",", metaKey: true, ctrlKey: false }, LIVE)).toBe(
			"open-settings",
		);
		expect(resolveShortcut({ key: ",", metaKey: true, ctrlKey: false }, { ...IDLE, modalOpen: true })).toBe(
			"open-settings",
		);
	});

	it("leaves an unmodified comma alone", () => {
		expect(resolveShortcut({ key: ",", metaKey: false, ctrlKey: false }, IDLE)).toBeNull();
	});

	it("no longer reaches the wizard — it is first-run only now", () => {
		const action = resolveShortcut({ key: ",", metaKey: true, ctrlKey: false }, IDLE);
		expect(action).not.toBe("open-wizard");
	});
});

describe("resolveShortcut — command palette (TD-1707)", () => {
	it("opens on ⌘K (meta) and Ctrl+K", () => {
		expect(resolveShortcut({ key: "k", metaKey: true, ctrlKey: false }, IDLE)).toBe(
			"open-palette",
		);
		expect(resolveShortcut({ key: "k", metaKey: false, ctrlKey: true }, IDLE)).toBe(
			"open-palette",
		);
	});

	it("opens with caps lock or shift on the key", () => {
		expect(resolveShortcut({ key: "K", metaKey: true, ctrlKey: false }, IDLE)).toBe(
			"open-palette",
		);
	});

	it("opens over a running turn and over an open pane", () => {
		expect(resolveShortcut({ key: "k", metaKey: true, ctrlKey: false }, LIVE)).toBe(
			"open-palette",
		);
		expect(
			resolveShortcut({ key: "k", metaKey: true, ctrlKey: false }, { ...IDLE, modalOpen: true }),
		).toBe("open-palette");
	});

	it("leaves a bare k alone so typing works", () => {
		expect(resolveShortcut({ key: "k", metaKey: false, ctrlKey: false }, IDLE)).toBeNull();
	});
});

describe("resolveShortcut — Design mode (TD-3403)", () => {
	it("toggles on ⌘⇧D and Ctrl+Shift+D", () => {
		expect(
			resolveShortcut({ key: "d", metaKey: true, ctrlKey: false, shiftKey: true }, IDLE),
		).toBe("toggle-design");
		expect(
			resolveShortcut({ key: "D", metaKey: false, ctrlKey: true, shiftKey: true }, IDLE),
		).toBe("toggle-design");
	});

	it("leaves ⌘D without shift alone so typing and browser bookmarks work", () => {
		expect(resolveShortcut({ key: "d", metaKey: true, ctrlKey: false }, IDLE)).toBeNull();
		expect(resolveShortcut({ key: "d", metaKey: false, ctrlKey: false, shiftKey: true }, IDLE)).toBeNull();
	});
});

describe("resolveShortcut — computer-use kill-switch (TD-3404)", () => {
	it("stops on ⌘. (meta) and Ctrl+.", () => {
		expect(resolveShortcut({ key: ".", metaKey: true, ctrlKey: false }, IDLE)).toBe(
			"stop-computer-use",
		);
		expect(resolveShortcut({ key: ".", metaKey: false, ctrlKey: true }, IDLE)).toBe(
			"stop-computer-use",
		);
	});

	it("works over a running turn and over an open pane", () => {
		expect(resolveShortcut({ key: ".", metaKey: true, ctrlKey: false }, LIVE)).toBe(
			"stop-computer-use",
		);
		expect(
			resolveShortcut({ key: ".", metaKey: true, ctrlKey: false }, { ...IDLE, modalOpen: true }),
		).toBe("stop-computer-use");
	});

	it("leaves a bare period alone so typing works", () => {
		expect(resolveShortcut({ key: ".", metaKey: false, ctrlKey: false }, IDLE)).toBeNull();
	});
});

describe("resolveShortcut — stepping between sessions (2026-09)", () => {
	it("steps down on ⌘⌥↓ and up on ⌘⌥↑, on either modifier", () => {
		expect(
			resolveShortcut({ key: "ArrowDown", metaKey: true, ctrlKey: false, altKey: true }, IDLE),
		).toBe("next-session");
		expect(
			resolveShortcut({ key: "ArrowUp", metaKey: false, ctrlKey: true, altKey: true }, IDLE),
		).toBe("prev-session");
	});

	it("leaves the arrows alone without Option, so the composer keeps ⌘↑ and ⌘↓", () => {
		expect(resolveShortcut({ key: "ArrowDown", metaKey: true, ctrlKey: false }, IDLE)).toBeNull();
		expect(
			resolveShortcut({ key: "ArrowUp", metaKey: true, ctrlKey: false, altKey: false }, IDLE),
		).toBeNull();
		expect(resolveShortcut({ key: "ArrowDown", metaKey: false, ctrlKey: false, altKey: true }, IDLE)).toBeNull();
	});

	it("works while a turn runs — switching away is not cancelling", () => {
		expect(
			resolveShortcut({ key: "ArrowDown", metaKey: true, ctrlKey: false, altKey: true }, LIVE),
		).toBe("next-session");
	});
});

describe("sessionSlot — ⌘1…⌘9 (2026-09)", () => {
	it("names the slot for ⌘ or Ctrl plus a digit", () => {
		expect(sessionSlot({ key: "1", metaKey: true, ctrlKey: false })).toBe(1);
		expect(sessionSlot({ key: "9", metaKey: false, ctrlKey: true })).toBe(9);
		expect(resolveShortcut({ key: "3", metaKey: true, ctrlKey: false }, IDLE)).toBe("pick-session");
	});

	it("ignores zero, letters, and a digit typed on its own", () => {
		expect(sessionSlot({ key: "0", metaKey: true, ctrlKey: false })).toBeNull();
		expect(sessionSlot({ key: "k", metaKey: true, ctrlKey: false })).toBeNull();
		expect(sessionSlot({ key: "4", metaKey: false, ctrlKey: false })).toBeNull();
		expect(resolveShortcut({ key: "4", metaKey: false, ctrlKey: false }, IDLE)).toBeNull();
	});

	it("stays out of shifted and Option chords", () => {
		expect(sessionSlot({ key: "1", metaKey: true, ctrlKey: false, shiftKey: true })).toBeNull();
		expect(sessionSlot({ key: "1", metaKey: true, ctrlKey: false, altKey: true })).toBeNull();
	});
});

describe("resolveShortcut — everything else", () => {
	it("returns null for unrelated keys", () => {
		expect(resolveShortcut({ key: "a", metaKey: false, ctrlKey: false }, LIVE)).toBeNull();
		expect(resolveShortcut({ key: "Enter", metaKey: true, ctrlKey: false }, LIVE)).toBeNull();
	});
});
