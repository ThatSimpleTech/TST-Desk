// @vitest-environment jsdom
//
// TD-5237: Settings → Model lists the catalog with Duplicate, Rename and
// Remove. Each button sends one message; the daemon's reply is the truth.

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

import ModelCatalog from "./components/ModelCatalog.svelte";
import { copyLabel, LABEL_MAX, renameTarget } from "./model-catalog";
import { settings } from "./settings.svelte.js";

let component: ReturnType<typeof mount> | null = null;

function emit(event: Partial<DaemonEventUnion> & { type: string }): void {
	for (const sub of [...mocks.subs]) sub(event as DaemonEventUnion);
	flushSync();
}

function open(): HTMLElement {
	settings.presets = ["local", "hosted"];
	settings.presetLabels = { local: "Local", hosted: "Hosted" };
	settings.presetDescriptions = { hosted: "A provider's model" };
	settings.activePreset = "hosted";
	const target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(ModelCatalog, { target });
	flushSync();
	return target;
}

function click(root: HTMLElement, name: string): void {
	const button = root.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`);
	if (button === null) throw new Error(`no button named ${name}`);
	button.click();
	flushSync();
}

function names(root: HTMLElement): string[] {
	return [...root.querySelectorAll("button")].map((b) => b.getAttribute("aria-label") ?? "");
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
	mocks.sendOk = true;
	mocks.subs.clear();
	settings.presets = [];
	settings.presetLabels = {};
	settings.presetDescriptions = {};
	settings.activePreset = null;
});

describe("copyLabel", () => {
	it("adds copy, then a number when that is taken", () => {
		expect(copyLabel("Local", ["Local"])).toBe("Local copy");
		expect(copyLabel("Local", ["Local", "local COPY"])).toBe("Local copy 2");
		expect(copyLabel("Local", ["Local copy", "Local copy 2"])).toBe("Local copy 3");
	});

	it("cuts a long label so the copy still fits", () => {
		const made = copyLabel("x".repeat(LABEL_MAX), []);
		expect(made).toHaveLength(LABEL_MAX);
		expect(made.endsWith(" copy")).toBe(true);
	});
});

describe("renameTarget", () => {
	it("is the trimmed label, or null when there is nothing to save", () => {
		expect(renameTarget("  House  ", "Local")).toBe("House");
		expect(renameTarget("Local", "Local")).toBeNull();
		expect(renameTarget("   ", "Local")).toBeNull();
		expect(renameTarget("x".repeat(LABEL_MAX + 1), "Local")).toBeNull();
	});
});

describe("Settings → Model → What you have added", () => {
	it("lists every model and marks the one new chats use", () => {
		const root = open();
		const rows = [...root.querySelectorAll("li")].map((li) => li.textContent ?? "");
		expect(rows).toHaveLength(2);
		expect(rows[0]).toContain("Local");
		expect(rows[0]).not.toContain("New chats use this");
		expect(rows[1]).toContain("Hosted");
		expect(rows[1]).toContain("New chats use this");
		expect(rows[1]).toContain("A provider's model");
	});

	it("shows nothing before a model is set up", () => {
		const target = document.createElement("div");
		document.body.appendChild(target);
		component = mount(ModelCatalog, { target });
		flushSync();
		expect(target.textContent?.trim()).toBe("");
	});

	it("Duplicate sends the source and a label no model has", () => {
		const root = open();
		click(root, "Duplicate Local");
		expect(mocks.sent).toEqual([
			{ type: "duplicate_preset", source: "local", label: "Local copy" },
		]);
	});

	it("holds the other buttons until the daemon answers", () => {
		const root = open();
		click(root, "Duplicate Local");
		const hosted = root.querySelector<HTMLButtonElement>('button[aria-label="Remove Hosted"]');
		expect(hosted?.disabled).toBe(true);
		emit({ type: "setup_state" });
		expect(
			root.querySelector<HTMLButtonElement>('button[aria-label="Remove Hosted"]')?.disabled,
		).toBe(false);
	});

	it("Rename sends the new label and keeps the key", () => {
		const root = open();
		click(root, "Rename Local");
		const input = root.querySelector<HTMLInputElement>('input[aria-label="New name for Local"]');
		expect(input?.value).toBe("Local");
		input!.value = "  On this computer ";
		input!.dispatchEvent(new Event("input", { bubbles: true }));
		flushSync();
		click(root, "Save name for Local");
		expect(mocks.sent).toEqual([
			{ type: "set_preset_label", name: "local", label: "On this computer" },
		]);
	});

	it("Rename with the same label, or cancelled, sends nothing", () => {
		const root = open();
		click(root, "Rename Local");
		expect(
			root.querySelector<HTMLButtonElement>('button[aria-label="Save name for Local"]')?.disabled,
		).toBe(true);
		click(root, "Cancel renaming Local");
		expect(mocks.sent).toEqual([]);
		expect(names(root)).toContain("Rename Local");
	});

	it("Remove asks first, and Keep sends nothing", () => {
		const root = open();
		click(root, "Remove Hosted");
		expect(mocks.sent).toEqual([]);
		expect(root.textContent).toContain("Remove Hosted?");
		click(root, "Keep Hosted");
		expect(mocks.sent).toEqual([]);
		click(root, "Remove Hosted");
		click(root, "Yes, remove Hosted");
		expect(mocks.sent).toEqual([{ type: "delete_preset", name: "hosted" }]);
	});

	it("shows why the daemon refused, and lets the person try again", () => {
		const root = open();
		click(root, "Remove Local");
		click(root, "Yes, remove Local");
		emit({
			type: "error",
			code: "bad_request",
			message: "Could not remove 'local': another model in the file reuses part of it.",
		});
		expect(root.querySelector('[role="alert"]')?.textContent).toContain("reuses part of it");
		expect(
			root.querySelector<HTMLButtonElement>('button[aria-label="Remove Local"]')?.disabled,
		).toBe(false);
	});

	it("does not wait on a message that was never sent", () => {
		const root = open();
		mocks.sendOk = false;
		click(root, "Duplicate Local");
		expect(
			root.querySelector<HTMLButtonElement>('button[aria-label="Duplicate Hosted"]')?.disabled,
		).toBe(false);
	});

	it("ignores an error that belongs to something else", () => {
		const root = open();
		emit({ type: "error", code: "bad_request", message: "not ours" });
		expect(root.querySelector('[role="alert"]')).toBeNull();
	});
});
