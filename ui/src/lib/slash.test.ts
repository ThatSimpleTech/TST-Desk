// Slash-command matching tests (TD-4501): trigger, ranking, insertion.
//
// Pure module — same node-environment shape as palette.test.ts, which this
// reuses for scoring.

import { describe, expect, it } from "vitest";
import type { CommandSummary, SkillSummary } from "./protocol";
import {
	entrySourceLabel,
	insertCommand,
	insertSlash,
	matchCommand,
	matchSkill,
	nextSlashIndex,
	rankCommands,
	rankEntries,
	slashQuery,
	sourceLabel,
} from "./slash";

function command(name: string, description: string | null = null): CommandSummary {
	return {
		name,
		source: "workspace",
		description,
		body: `${name}\n`,
		line_count: 1,
	};
}

function skill(
	name: string,
	description: string | null = null,
	whenToUse: string | null = null,
): SkillSummary {
	return { name, source: "workspace", description, when_to_use: whenToUse, line_count: 1 };
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

describe("skills in the menu (TD-4502)", () => {
	const commands = [command("deploy", "Ship it"), command("review")];
	const skills = [skill("deploy", "Ship it by playbook"), skill("triage", null, "a bug lands")];

	it("matchSkill scores a name above description and whenToUse", () => {
		const named = matchSkill("ship", skill("ship", "ship it"));
		const described = matchSkill("ship", skill("other", "ship it"));
		expect(named).not.toBeNull();
		expect(described).not.toBeNull();
		expect(named! - described!).toBeGreaterThanOrEqual(40);
	});

	it("whenToUse is a match field of last resort", () => {
		expect(matchSkill("bug", skill("triage", null, "when a bug lands"))).not.toBeNull();
	});

	it("rankEntries merges both lists; an empty query keeps discovery order", () => {
		expect(rankEntries(commands, skills, "").map((e) => `${e.kind}:${e.name}`)).toEqual([
			"command:deploy",
			"command:review",
			"skill:deploy",
			"skill:triage",
		]);
	});

	it("a query ranks across kinds and drops non-matches", () => {
		expect(rankEntries(commands, skills, "tri").map((e) => e.name)).toEqual(["triage"]);
		expect(rankEntries(commands, skills, "zzz")).toEqual([]);
	});

	it("an exact tie keeps the command ahead of the same-named skill", () => {
		expect(
			rankEntries([command("deploy")], [skill("deploy")], "deploy").map((e) => e.kind),
		).toEqual(["command", "skill"]);
	});

	it("insertSlash is insertCommand for any row", () => {
		const row = { kind: "skill", name: "triage", source: "user", description: null } as const;
		expect(insertSlash(row)).toBe("/triage ");
		expect(insertSlash({ ...row, kind: "command" })).toBe("/triage ");
	});

	it("the source tag says what a skill is; commands stay as they were", () => {
		expect(entrySourceLabel({ kind: "command", name: "deploy", source: "workspace", description: null })).toBe(
			"workspace",
		);
		expect(entrySourceLabel({ kind: "skill", name: "deploy", source: "workspace", description: null })).toBe(
			"skill · workspace",
		);
		expect(entrySourceLabel({ kind: "skill", name: "x", source: "user_fallback", description: null })).toContain(
			".claude",
		);
	});
});
