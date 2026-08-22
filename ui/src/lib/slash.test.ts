// Slash-command matching tests (TD-4501): trigger, ranking, insertion.
//
// Pure module — same node-environment shape as palette.test.ts, which this
// reuses for scoring.

import { describe, expect, it } from "vitest";
import type { CommandSummary } from "./protocol";
import { insertCommand, matchCommand, nextSlashIndex, rankCommands, slashQuery, sourceLabel } from "./slash";

function command(name: string, description: string | null = null): CommandSummary {
	return {
		name,
		source: "workspace",
		description,
		body: `${name}\n`,
		line_count: 1,
	};
}

describe("slashQuery", () => {
	it("opens on a leading slash", () => {
		expect(slashQuery("/")).toBe("");
		expect(slashQuery("/dep")).toBe("dep");
	});

	it("stays closed without the leading slash", () => {
		expect(slashQuery("deploy")).toBeNull();
		expect(slashQuery("run /deploy")).toBeNull();
		expect(slashQuery("")).toBeNull();
	});

	it("a space means prose (or arguments) and stands down", () => {
		expect(slashQuery("/deploy now")).toBeNull();
		expect(slashQuery("/ ")).toBeNull();
	});
});

describe("rankCommands", () => {
	const entries = [command("deploy", "Ship it"), command("review"), command("redeploy")];

	it("an empty query keeps discovery order", () => {
		expect(rankCommands(entries, "").map((c) => c.name)).toEqual(["deploy", "review", "redeploy"]);
	});

	it("a prefix ranks exact-prefix names first", () => {
		expect(rankCommands(entries, "de").map((c) => c.name)).toEqual(["deploy", "redeploy"]);
	});

	it("a description hit survives when the name doesn't match", () => {
		expect(rankCommands(entries, "ship")).toEqual([entries[0]]);
	});

	it("non-matches drop out entirely", () => {
		expect(rankCommands(entries, "zzz")).toEqual([]);
	});

	it("matchCommand scores a name above its own description", () => {
		const named = matchCommand("ship", command("ship", "ship it"));
		const described = matchCommand("ship", command("other", "ship it"));
		expect(named).not.toBeNull();
		expect(described).not.toBeNull();
		expect(named! - described!).toBeGreaterThanOrEqual(40);
	});
});

describe("insertCommand / nextSlashIndex / sourceLabel", () => {
	it("inserts with a trailing space ready for arguments", () => {
		expect(insertCommand(command("deploy"))).toBe("/deploy ");
	});

	it("selection wraps both ways", () => {
		expect(nextSlashIndex(0, -1, 3)).toBe(2);
		expect(nextSlashIndex(2, 1, 3)).toBe(0);
	});

	it("labels every source, including the fallbacks", () => {
		expect(sourceLabel("workspace")).toBe("workspace");
		expect(sourceLabel("user")).toBe("global");
		expect(sourceLabel("workspace_fallback")).toContain(".claude");
		expect(sourceLabel("user_fallback")).toContain(".claude");
	});
});
