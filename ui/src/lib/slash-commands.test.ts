// Composer `/` behaviour (TD-4501). The list is mocked — names come from
// the daemon, never invented here.

import { describe, expect, it } from "vitest";
import type { CommandEntry } from "./protocol";
import { filterCommands, insertCommandBody, slashQuery } from "./slash-commands";

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
