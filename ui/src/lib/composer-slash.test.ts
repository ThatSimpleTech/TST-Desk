// @vitest-environment jsdom
//
// TD-4501: the composer's slash menu against the real component and the
// real store, with only the connection seam mocked (the palette-store.test
// posture). Covers the story's wire: "/" lists daemon commands, Enter
// inserts (never sends), ⌘/Ctrl+Enter sends, Escape is *not* the
// composer's to eat — shortcuts.ts owns that layer.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => {
	const eventHandlers = new Set<(e: DaemonEventUnion) => void>();
	return {
		eventHandlers,
		sent: [] as ClientMessageUnion[],
	};
});

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.eventHandlers.add(handler);
		return () => {
			mocks.eventHandlers.delete(handler);
		};
	},
	onConnectionState: () => () => {},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
	ws: { state: "connected" },
}));

import Composer from "./components/chat/Composer.svelte";
import { dismissSlashMenu, resetCommandMenu, startCommandMenu } from "./commands-store.svelte.js";
import type { AttachmentLimits } from "./protocol";

const LIMITS: AttachmentLimits = {
	max_count: 5,
	max_file_bytes: 100_000,
	max_total_bytes: 200_000,
};

let app: ReturnType<typeof mount> | null = null;
const submitted: { text: string }[] = [];

beforeEach(() => {
	// The store's reducer must be live before any commands_list lands.
	resetCommandMenu();
	startCommandMenu();
});

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	submitted.length = 0;
	mocks.sent.length = 0;
	mocks.eventHandlers.clear();
	resetCommandMenu();
});

function emit(event: DaemonEventUnion): void {
	for (const handler of mocks.eventHandlers) handler(event);
}

async function renderComposer(): Promise<HTMLTextAreaElement> {
	app = mount(Composer, {
		target: document.body,
		props: {
			limits: LIMITS,
			sessionId: "sess-1",
			onsubmit: (text: string) => {
				submitted.push({ text });
			},
		},
	});
	await tick();
	const textarea = document.body.querySelector("textarea");
	if (!(textarea instanceof HTMLTextAreaElement)) throw new Error("textarea did not mount");
	return textarea;
}

/** Type by setting the bindable value through a real input event. */
async function type(textarea: HTMLTextAreaElement, text: string): Promise<void> {
	textarea.value = text;
	textarea.dispatchEvent(new Event("input", { bubbles: true }));
	await tick();
}

function press(textarea: HTMLTextAreaElement, key: string, init: KeyboardEventInit = {}): void {
	textarea.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init }));
}

describe("Composer slash menu (TD-4501)", () => {
	it("fetches both listings when a session is attached", async () => {
		await renderComposer();
		expect(mocks.sent).toEqual([
			{ type: "list_commands", session_id: "sess-1" },
			{ type: "list_skills", session_id: "sess-1" },
		]);
	});

	it("lists commands on / and inserts on Enter without sending", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: "Ship it", body: "Deploy\n", line_count: 1 },
				{ name: "review", source: "user_fallback", description: null, body: "Review\n", line_count: 1 },
			],
		} as DaemonEventUnion);

		await type(textarea, "/");
		const menu = document.body.querySelector("#slash-menu");
		expect(menu).not.toBeNull();
		const options = document.body.querySelectorAll('[role="option"]');
		expect(options).toHaveLength(2);

		press(textarea, "ArrowDown"); // highlight review
		await tick();
		press(textarea, "Enter");
		await tick();

		expect((textarea as unknown as { value: string }).value).toBe("/review ");
		expect(submitted).toEqual([]); // default insert, never send
	});

	it("⌘/Ctrl+Enter sends the chosen command straight away", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: null, body: "Deploy\n", line_count: 1 },
			],
		} as DaemonEventUnion);

		await type(textarea, "/dep");
		press(textarea, "Enter", { metaKey: true });
		await tick();

		expect(submitted).toEqual([{ text: "/deploy" }]);
		expect((textarea as unknown as { value: string }).value).toBe("");
	});

	it("a space stands the menu down; prose types normally", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: null, body: "Deploy\n", line_count: 1 },
			],
		} as DaemonEventUnion);
		await type(textarea, "/");
		expect(document.body.querySelector("#slash-menu")).not.toBeNull();
		await type(textarea, "/deploy ");
		expect(document.body.querySelector("#slash-menu")).toBeNull();
	});

	it("Escape belongs to shortcuts.ts: the composer neither eats nor needs it", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: null, body: "Deploy\n", line_count: 1 },
			],
		} as DaemonEventUnion);
		await type(textarea, "/");

		const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
		textarea.dispatchEvent(event);
		await tick();
		// Not intercepted — it must reach the shell's layer resolver.
		expect(event.defaultPrevented).toBe(false);
		expect(document.body.querySelector("#slash-menu")).not.toBeNull();

		// What shortcuts.ts triggers (AppShell → dismissSlashMenu) closes it.
		dismissSlashMenu();
		await tick();
		expect(document.body.querySelector("#slash-menu")).toBeNull();
	});

	it("clicking an option inserts without stealing the caret", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: null, body: "Deploy\n", line_count: 1 },
			],
		} as DaemonEventUnion);
		await type(textarea, "/");

		const option = document.body.querySelector('[role="option"]');
		if (!(option instanceof HTMLButtonElement)) throw new Error("option not rendered");
		option.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
		option.click();
		await tick();

		expect((textarea as unknown as { value: string }).value).toBe("/deploy ");
	});

	it("skills ride the same menu with a skill source tag (TD-4502)", async () => {
		const textarea = await renderComposer();
		emit({
			type: "commands_list",
			seq: 1,
			commands: [
				{ name: "deploy", source: "workspace", description: null, body: "Deploy\n", line_count: 1 },
			],
		} as DaemonEventUnion);
		emit({
			type: "skills_list",
			seq: 1,
			skills: [
				{ name: "triage", source: "user", description: "Sort the queue", when_to_use: null, line_count: 3 },
			],
		} as DaemonEventUnion);

		await type(textarea, "/tri");
		const options = document.body.querySelectorAll('[role="option"]');
		expect(options).toHaveLength(1);
		expect(options[0].textContent).toContain("/triage");
		expect(options[0].textContent).toContain("skill · global");

		// Choosing one inserts like any command; the daemon expands the body.
		press(textarea, "Enter");
		await tick();
		expect((textarea as unknown as { value: string }).value).toBe("/triage ");
		expect(submitted).toEqual([]);
	});
});
