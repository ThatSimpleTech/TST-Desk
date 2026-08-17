// Shortcut mapping tests (TD-1609): Escape layer order and the ⌘, wizard
// reopen, including the combos that must stay inert.

import { describe, expect, it } from "vitest";
import { resolveShortcut, type ShortcutContext } from "./shortcuts";

const IDLE: ShortcutContext = { workspaceMenuOpen: false, modalOpen: false, turnLive: false };
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

	it("does nothing when a modal is open — Escape never reaches through it", () => {
		expect(resolveShortcut(esc(), { ...LIVE, modalOpen: true })).toBeNull();
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

describe("resolveShortcut — everything else", () => {
	it("returns null for unrelated keys", () => {
		expect(resolveShortcut({ key: "a", metaKey: false, ctrlKey: false }, LIVE)).toBeNull();
		expect(resolveShortcut({ key: "Enter", metaKey: true, ctrlKey: false }, LIVE)).toBeNull();
	});
});
