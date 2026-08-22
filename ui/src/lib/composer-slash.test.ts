// @vitest-environment jsdom
//
// Tests for the composer's slash menu (TD-4501, TD-4502). The menu is real
// markup inside Composer, so the tests mount a harness and drive the
// textarea's keydown handlers — the same path a keyboard takes.
//
// The commands store is stubbed at its module seam: items and skills are
// fixed before mount (a plain object, so no reactivity is promised) and
// requestCommands is recorded rather than sent. matchCommands/matchSkills
// stay the real thing.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import type { CommandEntry, SkillSummary } from "./protocol";

const mocks = vi.hoisted(() => ({
	requested: [] as (string | null)[],
	items: [
		{ name: "deploy", source: "workspace", path: "/w/.tst/commands/deploy.md", fallback: false },
		{ name: "review", source: "user", path: "~/.tstdesk/commands/review.md", fallback: false },
	] as CommandEntry[],
	skills: [
		{
			name: "deep-dive",
			source: "workspace",
			fallback: false,
			description: "read a module end to end",
			when_to_use: "",
		},
		{
			name: "triage",
			source: "user",
			fallback: true,
			description: "",
			when_to_use: "",
		},
	] as SkillSummary[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => {},
	sendToDaemon: () => true,
}));

vi.mock("./commands.svelte.js", async (importOriginal) => {
	const actual = await importOriginal<typeof import("./commands.svelte.js")>();
	return {
		...actual,
		slashCommands: {
			get items() {
				return mocks.items;
			},
			get skills() {
				return mocks.skills;
			},
		},
		requestCommands: (workspacePath: string | null) => {
			mocks.requested.push(workspacePath);
		},
	};
});

import ComposerHarness from "./components/chat/Composer.harness.svelte";

type Harness = {
	type(text: string): void;
	draft(): string;
	submits(): { text: string; attachments: readonly unknown[] }[];
};

let app: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	mocks.requested = [];
});

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
});

function harness(): Harness {
	if (app === null) throw new Error("composer not mounted");
	return app as unknown as Harness;
}

function textarea(): HTMLTextAreaElement {
	const el = document.body.querySelector("textarea");
	if (!(el instanceof HTMLTextAreaElement)) throw new Error("textarea not mounted");
	return el;
}

function menu(): Element | null {
	return document.body.querySelector(".slash-menu");
}

function items(): NodeListOf<HTMLButtonElement> {
	return document.body.querySelectorAll(".slash-item");
}

function press(key: string, over: KeyboardEventInit = {}): boolean {
	const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...over });
	textarea().dispatchEvent(event);
	return event.defaultPrevented;
}

async function mounted(): Promise<void> {
	app = mount(ComposerHarness, { target: document.body });
	await tick();
}

describe("opening the menu", () => {
	it('lists the commands and skills once "/" is typed', async () => {
		await mounted();
		harness().type("/");
		await tick();
		expect(menu()).not.toBeNull();
		const names = [...items()].map((b) => b.querySelector(".slash-name")?.textContent);
		expect(names).toEqual(["/deploy", "/review", "/deep-dive", "/triage"]);
		// The listing is pulled per-open, keyed on the open workspace —
		// one ask covers both halves of the menu.
		expect(mocks.requested).toEqual(["/w"]);
	});

	it("stays closed for text that is not a slash query", async () => {
		await mounted();
		harness().type("/deploy now");
		await tick();
		expect(menu()).toBeNull();
	});

	it("shows nothing when nothing matches", async () => {
		await mounted();
		harness().type("/zz");
		await tick();
		expect(menu()).toBeNull();
	});
});

describe("skills share the menu (TD-4502)", () => {
	it("lists skills after the commands, tagged as skills", async () => {
		await mounted();
		harness().type("/");
		await tick();
		const names = [...items()].map((b) => b.querySelector(".slash-name")?.textContent);
		expect(names).toEqual(["/deploy", "/review", "/deep-dive", "/triage"]);
		const sources = [...items()].map((b) => b.querySelector(".slash-source")?.textContent?.trim());
		expect(sources).toEqual(["workspace", "user", "skill · workspace", "skill · claude"]);
	});

	it("a skill query narrows to the skill; Enter inserts it like a command", async () => {
		await mounted();
		harness().type("/deep");
		await tick();
		const names = [...items()].map((b) => b.querySelector(".slash-name")?.textContent);
		expect(names).toEqual(["/deep-dive"]);
		const prevented = press("Enter");
		await tick();
		expect(prevented).toBe(true);
		expect(harness().draft()).toBe("/deep-dive ");
	});
});

describe("insert is the default", () => {
	it("Enter inserts the highlighted command with a trailing space", async () => {
		await mounted();
		harness().type("/de");
		await tick();
		const prevented = press("Enter");
		await tick();
		expect(prevented).toBe(true);
		expect(harness().draft()).toBe("/deploy ");
		expect(harness().submits()).toEqual([]);
		// Inserting empties the query, which closes the menu.
		expect(menu()).toBeNull();
	});

	it("clicking an item inserts it too", async () => {
		await mounted();
		harness().type("/");
		await tick();
		items()[1].click();
		await tick();
		expect(harness().draft()).toBe("/review ");
	});
});

describe("Alt+Enter sends as typed", () => {
	it("submits the invocation instead of inserting", async () => {
		await mounted();
		harness().type("/deploy prod");
		await tick();
		const prevented = press("Enter", { altKey: true });
		await tick();
		expect(prevented).toBe(true);
		expect(harness().submits()).toEqual([{ text: "/deploy prod", attachments: [] }]);
	});
});

describe("keyboard navigation", () => {
	it("arrows move the highlight with wraparound; Enter picks the highlighted one", async () => {
		await mounted();
		harness().type("/"); // two commands, then two skills
		await tick();
		press("ArrowDown"); // 0 → 1
		await tick();
		expect(items()[1].classList.contains("active")).toBe(true);
		press("ArrowUp"); // 1 → 0
		await tick();
		expect(items()[0].classList.contains("active")).toBe(true);
		press("ArrowUp"); // 0 → wraps to the last row, a skill
		await tick();
		expect(items()[3].classList.contains("active")).toBe(true);
		press("ArrowDown"); // 3 → wraps to 0
		await tick();
		expect(items()[0].classList.contains("active")).toBe(true);
		press("ArrowDown"); // 0 → 1
		await tick();
		press("Enter");
		await tick();
		expect(harness().draft()).toBe("/review ");
	});

	it("Escape closes the menu until the query goes away", async () => {
		await mounted();
		harness().type("/de");
		await tick();
		expect(menu()).not.toBeNull();
		expect(press("Escape")).toBe(true);
		await tick();
		expect(menu()).toBeNull();

		// Still a live query, so the next keystroke must not reopen what
		// was just dismissed.
		harness().type("/dep");
		await tick();
		expect(menu()).toBeNull();

		// Query gone — the latch clears, a fresh slash reopens.
		harness().type("/dep now");
		await tick();
		harness().type("/re");
		await tick();
		expect(menu()).not.toBeNull();
	});
});
