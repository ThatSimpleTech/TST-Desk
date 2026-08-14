// open-file wrapper tests (TD-1201).

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-opener", () => ({
	openPath: vi.fn(async () => {}),
}));

import { openPath } from "@tauri-apps/plugin-opener";
import { isTauri, openInEditor } from "./open-file";

function withShell(): void {
	(globalThis as { window?: unknown }).window = { __TAURI_INTERNALS__: {} };
}

afterEach(() => {
	delete (globalThis as { window?: unknown }).window;
	vi.mocked(openPath).mockClear();
});

describe("isTauri", () => {
	it("is false without a window (node/test env)", () => {
		expect(isTauri()).toBe(false);
	});

	it("is true when Tauri internals are present", () => {
		withShell();
		expect(isTauri()).toBe(true);
	});
});

describe("openInEditor", () => {
	it("no-ops outside the shell", async () => {
		expect(await openInEditor("/ws/AGENTS.md")).toBe(false);
		expect(openPath).not.toHaveBeenCalled();
	});

	it("opens via the opener plugin inside the shell", async () => {
		withShell();
		expect(await openInEditor("/ws/AGENTS.md")).toBe(true);
		expect(openPath).toHaveBeenCalledWith("/ws/AGENTS.md");
	});

	it("returns false when the shell open fails", async () => {
		withShell();
		vi.mocked(openPath).mockRejectedValueOnce(new Error("no handler"));
		expect(await openInEditor("/ws/AGENTS.md")).toBe(false);
	});
});
