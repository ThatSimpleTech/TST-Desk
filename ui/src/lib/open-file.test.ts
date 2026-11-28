// @vitest-environment jsdom
//
// open-file wrapper tests (TD-1201, PKG-12). Every file open goes through the
// host's open_path command, which checks the path; the window holds no
// open-path grant of its own. This used to mock the opener plugin, which is
// how a permission that allowed nothing passed for three releases.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const core = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => core);

import { isTauri, openInEditor, openSessionPath } from "./open-file";
import { clearNotifications, toasts } from "./notifications.svelte.js";

function withShell(): void {
	(window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

beforeEach(() => {
	clearNotifications();
	core.invoke.mockReset();
});

afterEach(() => {
	delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe("isTauri", () => {
	it("is false outside the shell", () => {
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
		expect(core.invoke).not.toHaveBeenCalled();
	});

	it("asks the host's open_path inside the shell", async () => {
		withShell();
		core.invoke.mockResolvedValue(undefined);
		expect(await openInEditor("/ws/AGENTS.md")).toBe(true);
		expect(core.invoke).toHaveBeenCalledWith("open_path", { path: "/ws/AGENTS.md" });
		expect(toasts).toHaveLength(0);
	});

	it("a refusal is shown with the host's own sentence, not swallowed", async () => {
		withShell();
		core.invoke.mockRejectedValue(
			"setup.sh is a program or launcher, so it is not opened from here. Open its folder instead.",
		);
		expect(await openInEditor("/ws/setup.sh")).toBe(false);
		expect(toasts.map((t) => [t.title, t.body])).toEqual([
			[
				"Couldn't open the file",
				"setup.sh is a program or launcher, so it is not opened from here. Open its folder instead.",
			],
		]);
	});

	it("a failure with no message still says which file", async () => {
		withShell();
		core.invoke.mockRejectedValue(new Error("x"));
		expect(await openInEditor("/ws/a.md")).toBe(false);
		expect(toasts[0].body).toBe("Could not open /ws/a.md.");
	});

	it("never opens another computer's path", async () => {
		withShell();
		expect(await openSessionPath("s1:mch_0123456789ab", "/elsewhere/x.md")).toBe(false);
		expect(core.invoke).not.toHaveBeenCalled();
	});
});
