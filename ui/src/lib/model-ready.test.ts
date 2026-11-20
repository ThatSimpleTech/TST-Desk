// @vitest-environment jsdom
//
// TD-5240: the picker asks the daemon, each time it opens, which models can
// run now, and shows a model's problem on its row before the click.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	sent: [] as ClientMessageUnion[],
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
		sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
	};
});
vi.mock("./sessions.svelte.js", () => ({ newSession: () => true }));

import ModelPicker from "./components/chat/ModelPicker.svelte";
import { problemWith, readiness, resetReadiness } from "./model-ready.svelte.js";
import { resetSession, session } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: Partial<DaemonEventUnion> & { type: string }): void {
	for (const sub of [...mocks.subs]) sub(event as DaemonEventUnion);
	flushSync();
}

const chip = (): HTMLButtonElement =>
	document.querySelector<HTMLButtonElement>('[aria-label="Model and effort"]')!;

function row(label: string): HTMLButtonElement {
	const found = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find(
		(button) => button.querySelector(".label")?.textContent?.includes(label),
	);
	if (found === undefined) throw new Error(`no row for ${label}`);
	return found;
}

beforeEach(async () => {
	resetSession();
	resetSettings();
	resetReadiness();
	mocks.sent = [];
	session.sessionId = "s1";
	session.preset = "api";
	session.engine = "native";
	settings.hasModel = true;
	settings.activePreset = "api";
	settings.presets = ["api", "local"];
	settings.presetLabels = { api: "Hosted", local: "Laptop" };
	settings.presetDescriptions = { local: "Served on this computer" };
	app = mount(ModelPicker, { target: document.body });
	await tick();
});

afterEach(async () => {
	if (app !== null) await unmount(app);
	app = null;
	document.body.replaceChildren();
	mocks.subs.clear();
	resetReadiness();
	resetSession();
	resetSettings();
});

describe("the picker's readiness check", () => {
	it("asks each time the picker opens, and not when it closes", () => {
		chip().click();
		flushSync();
		expect(mocks.sent).toEqual([{ type: "check_presets" }]);
		chip().click();
		flushSync();
		expect(mocks.sent).toHaveLength(1);
		chip().click();
		flushSync();
		expect(mocks.sent).toEqual([{ type: "check_presets" }, { type: "check_presets" }]);
	});

	it("shows a model's problem on its row in place of its description", () => {
		chip().click();
		flushSync();
		expect(row("Laptop").querySelector(".sub")?.textContent).toBe("Served on this computer");
		emit({
			type: "preset_readiness",
			rows: [
				{ preset: "api", status: "ready", detail: "" },
				{
					preset: "local",
					status: "server_down",
					detail: "Nothing is answering at 127.0.0.1:8000. Start the model server.",
				},
			],
		});
		const sub = row("Laptop").querySelector(".sub");
		expect(sub?.textContent).toContain("Nothing is answering at 127.0.0.1:8000");
		expect(sub?.classList.contains("sub--problem")).toBe(true);
		expect(row("Hosted").querySelector(".sub--problem")).toBeNull();
		// A warning, not a wall: the model can still be picked.
		expect(row("Laptop").disabled).toBe(false);
	});

	it("clears the problem when the daemon next says the model is ready", () => {
		chip().click();
		emit({
			type: "preset_readiness",
			rows: [{ preset: "local", status: "no_key", detail: "No key is stored for House." }],
		});
		expect(problemWith("local")).toBe("No key is stored for House.");
		emit({ type: "preset_readiness", rows: [{ preset: "local", status: "ready" }] });
		expect(problemWith("local")).toBe("");
		expect(row("Laptop").querySelector(".sub")?.textContent).toBe("Served on this computer");
	});

	it("says nothing about a model the daemon said nothing about", () => {
		emit({ type: "preset_readiness", rows: [] });
		expect(readiness.rows).toEqual({});
		expect(problemWith("api")).toBe("");
		emit({ type: "setup_state" });
		expect(readiness.rows).toEqual({});
	});
});
