// @vitest-environment jsdom
//
// TD-5258: Settings → Engine can put the Grok CLI on the model list. The
// button is always there; it is held, with the reason, where the CLI is
// missing, and gives way to a line once Grok is on the list.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
	sendOk: true,
	subs: new Set<(event: DaemonEventUnion) => void>(),
}));

vi.mock("./connection-status.svelte.js", () => {
	const onDaemonEvent = (handler: (event: DaemonEventUnion) => void) => {
		mocks.subs.add(handler);
		return () => mocks.subs.delete(handler);
	};
	return {
		onEvent: onDaemonEvent,
		onDaemonEvent,
		sendToDaemon: (msg: ClientMessageUnion) => {
			if (mocks.sendOk) mocks.sent.push(msg);
			return mocks.sendOk;
		},
	};
});

import GrokOnPicker from "./components/GrokOnPicker.svelte";
import { settings } from "./settings.svelte.js";

let component: ReturnType<typeof mount> | null = null;
const BUTTON = "Add Grok to your models";

function open(grokAvailable: boolean): HTMLElement {
	settings.grokAvailable = grokAvailable;
	settings.presets = ["local"];
	settings.presetLabels = { local: "Local" };
	const target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(GrokOnPicker, { target });
	flushSync();
	return target;
}

function button(root: HTMLElement): HTMLButtonElement | null {
	return (
		[...root.querySelectorAll<HTMLButtonElement>("button")].find(
			(b) => b.textContent?.trim() === BUTTON,
		) ?? null
	);
}

function emit(event: Partial<DaemonEventUnion> & { type: string }): void {
	for (const sub of [...mocks.subs]) sub(event as DaemonEventUnion);
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
	mocks.sendOk = true;
	mocks.subs.clear();
	settings.grokAvailable = false;
	settings.presets = [];
	settings.presetLabels = {};
	settings.presetEngines = {};
});

describe("Settings → Engine → Grok on the model list", () => {
	it("sends one message when the Grok CLI is here", () => {
		const root = open(true);
		expect(button(root)?.disabled).toBe(false);
		button(root)!.click();
		flushSync();
		expect(mocks.sent).toEqual([{ type: "add_grok_preset", label: "Grok" }]);
		// Held until the daemon answers, so a second click cannot add a second.
		expect(button(root)?.disabled).toBe(true);
	});

	it("is held, with the reason, where the Grok CLI is not found", () => {
		const root = open(false);
		expect(button(root)).not.toBeNull();
		expect(button(root)?.disabled).toBe(true);
		expect(button(root)?.title).toContain("not found");
		expect(root.textContent).toContain("Install Grok Build");
		button(root)!.click();
		expect(mocks.sent).toEqual([]);
	});

	it("gives way to a line once Grok is on the list", () => {
		const root = open(true);
		button(root)!.click();
		settings.presets = ["grok", "local"];
		settings.presetLabels = { grok: "Grok", local: "Local" };
		settings.presetEngines = { grok: "grok" };
		emit({ type: "setup_state" });
		expect(button(root)).toBeNull();
		expect(root.textContent).toContain("Grok is on the model list");
	});

	it("shows why the daemon refused, and lets the person try again", () => {
		const root = open(true);
		button(root)!.click();
		emit({ type: "error", code: "grok_unavailable", message: "The Grok CLI was not found." });
		expect(root.querySelector('[role="alert"]')?.textContent).toContain("not found");
		expect(button(root)?.disabled).toBe(false);
	});

	it("does not wait on a message that was never sent", () => {
		const root = open(true);
		mocks.sendOk = false;
		button(root)!.click();
		flushSync();
		expect(button(root)?.disabled).toBe(false);
	});
});
