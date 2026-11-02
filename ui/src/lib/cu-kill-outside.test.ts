// @vitest-environment jsdom
//
// TD-5223: Stop is machine-wide. When another tool creates the stop-file,
// the daemon sends `cu_kill_state` unasked; the title-bar button and the
// ⌘. shortcut must reflect it. Nothing here was sent first.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import CuKillSwitch from "./components/CuKillSwitch.svelte";
import { cuKill, engageFromShortcut, resetCuKill, startCuKill } from "./cu-kill.svelte.js";
import { resolveShortcut } from "./shortcuts";

function outside(killed: boolean): void {
	mocks.handler?.({ type: "cu_kill_state", seq: 1, killed });
	flushSync();
}

let target: HTMLElement;
let app: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	resetCuKill();
	mocks.sent.length = 0;
	startCuKill();
	target = document.createElement("div");
	document.body.appendChild(target);
	app = mount(CuKillSwitch, { target });
	flushSync();
});

afterEach(() => {
	if (app) unmount(app);
	app = null;
	target.remove();
});

function button(): HTMLButtonElement {
	const el = target.querySelector("button");
	if (!el) throw new Error("no kill button");
	return el;
}

describe("stop engaged outside TST Desk", () => {
	it("the button shows stopped without anyone clicking it", () => {
		expect(button().getAttribute("aria-pressed")).toBe("false");
		outside(true);
		expect(mocks.sent).toEqual([]);
		expect(cuKill.killed).toBe(true);
		expect(button().getAttribute("aria-pressed")).toBe("true");
		expect(button().getAttribute("aria-label")).toBe("Resume computer use");
		expect(button().textContent).toContain("CU stopped");
	});

	it("resume from the button clears an outside stop", () => {
		outside(true);
		button().click();
		expect(mocks.sent).toEqual([{ type: "set_cu_kill", killed: false }]);
	});

	it("the button follows the file being removed outside too", () => {
		outside(true);
		outside(false);
		expect(button().getAttribute("aria-pressed")).toBe("false");
		expect(button().textContent).toContain("Stop CU");
	});

	it("⌘. while stopped from outside keeps it stopped (engage only)", () => {
		outside(true);
		const action = resolveShortcut(
			{ key: ".", metaKey: true, ctrlKey: false },
			{ turnLive: false, modalOpen: false, paletteOpen: false, workspaceMenuOpen: false },
		);
		expect(action).toBe("stop-computer-use");
		engageFromShortcut();
		expect(mocks.sent).toEqual([{ type: "set_cu_kill", killed: true }]);
		expect(cuKill.killed).toBe(true);
	});
});
