// Composer `/` behaviour (TD-4501). The list is mocked — names come from
// the daemon, never invented here.

import { describe, expect, it } from "vitest";
import type { CommandEntry, SkillStackEntry } from "./protocol";
import {
	BUILTIN_SLASH_ITEMS,
	filterCommands,
	insertCommandBody,
	mergeSlashItems,
	skillLoadMarker,
	slashQuery,
} from "./slash-commands";

const list: CommandEntry[] = [
	{
		name: "review",
		description: "Review the diff",
		source: "workspace",
		body: "Please review the staged changes.\n",
	},
	{
		name: "ship",
		description: "",
		source: "user",
		body: "Ship it.\n",
	},
];

describe("slashQuery", () => {
	it("opens on / at the start of the draft", () => {
		expect(slashQuery("/")).toBe("");
		expect(slashQuery("/rev")).toBe("rev");
	});

	it("opens after leading whitespace at the start", () => {
		expect(slashQuery("  /ship")).toBe("ship");
	});

	it("does not open when / is not the first token", () => {
		expect(slashQuery("hello /")).toBeNull();
		expect(slashQuery("x /rev")).toBeNull();
	});

	it("closes once a space follows the name", () => {
		expect(slashQuery("/review more")).toBeNull();
	});
});

describe("filterCommands", () => {
	it("filters the daemon list by prefix", () => {
		expect(filterCommands(list, "").map((c) => c.name)).toEqual(["review", "ship"]);
		expect(filterCommands(list, "re").map((c) => c.name)).toEqual(["review"]);
		expect(filterCommands(list, "z")).toEqual([]);
	});
});

describe("insertCommandBody", () => {
	it("replaces the /name token with the body (default insert)", () => {
		expect(insertCommandBody("/review", list[0]!.body)).toBe(
			"Please review the staged changes.\n",
		);
	});

	it("keeps leading whitespace", () => {
		expect(insertCommandBody("  /ship", list[1]!.body)).toBe("  Ship it.\n");
	});

	it("leaves a non-slash draft alone", () => {
		expect(insertCommandBody("hello", list[0]!.body)).toBe("hello");
	});
});

describe("mergeSlashItems", () => {
	const skills: SkillStackEntry[] = [
		{
			name: "review",
			description: "skill review",
			source: "workspace",
			loaded: false,
			tokens: 10,
		},
		{
			name: "draft",
			description: "Draft a reply",
			source: "user",
			loaded: false,
			tokens: 8,
		},
	];

	it("commands win on the same stem; skills that are not commands appear", () => {
		const merged = mergeSlashItems(list, skills);
		expect(merged.map((c) => c.name)).toEqual(["review", "ship", "draft"]);
		expect(merged[0]!.body).toBe(list[0]!.body);
		expect(merged[2]!.body).toBe(skillLoadMarker("draft"));
	});
});

describe("built-in slash items (TD-5109)", () => {
	it("lists /compact after commands and skills", () => {
		const items = mergeSlashItems(list, [], BUILTIN_SLASH_ITEMS);
		expect(items.map((c) => c.name)).toEqual(["review", "ship", "compact", "goal", "plan"]);
		expect(items[2]!.source).toBe("built-in");
		expect(items[3]!.source).toBe("built-in");
		expect(items[4]!.source).toBe("built-in");
		expect(filterCommands(items, "comp").map((c) => c.name)).toEqual(["compact"]);
	});

	it("inserts /compact with room for guidance, closing the picker", () => {
		const draft = insertCommandBody("/comp", BUILTIN_SLASH_ITEMS[0]!.body);
		expect(draft).toBe("/compact ");
		expect(slashQuery(draft)).toBeNull();
	});

	it("gives way to a workspace command of the same name", () => {
		const own = [{ ...list[0]!, name: "compact", body: "Squash my commits." }];
		const items = mergeSlashItems(own, [], BUILTIN_SLASH_ITEMS);
		expect(items.map((c) => c.name)).toEqual(["compact", "goal", "plan"]);
		expect(items[0]!.body).toBe("Squash my commits.");
		// TD-5123: so does /plan.
		const plan = mergeSlashItems([{ ...list[0]!, name: "plan", body: "Write PLAN.md." }], [], BUILTIN_SLASH_ITEMS);
		expect(plan.map((c) => [c.name, c.body])).toEqual([
			["plan", "Write PLAN.md."],
			["compact", "/compact "],
			["goal", "/goal "],
		]);
	});

	it("offers /goal with room for the statement (TD-5124)", () => {
		const items = mergeSlashItems([], [], BUILTIN_SLASH_ITEMS);
		const goal = filterCommands(items, "go");
		expect(goal.map((c) => c.name)).toEqual(["goal"]);
		const draft = insertCommandBody("/go", goal[0]!.body);
		expect(draft).toBe("/goal ");
		expect(slashQuery(draft)).toBeNull();
	});
});
