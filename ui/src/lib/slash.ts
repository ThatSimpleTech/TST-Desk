// Slash-command matching for the composer (TD-4501).
//
// Pure and rune-free like palette.ts: the trigger test, the ranking, and the
// insertion shape are unit-testable in the node vitest environment, while
// commands-store.svelte.ts holds the fetched listing and the menu state.
//
// Ranking reuses the palette's subsequence matcher — same list size, same
// feel, one matcher to reason about.

import { fuzzyScore, nextIndex } from "./palette";
import type { CommandSummary, SkillSummary } from "./protocol";

/** The draft's slash query, or null when no menu should show.
 *
 *  A leading "/" opens the menu; everything after it is the query until a
 *  whitespace lands — a space means the user is writing prose (or a command's
 *  arguments) and the menu stands down. */
export function slashQuery(draft: string): string | null {
	if (!draft.startsWith("/")) return null;
	const query = draft.slice(1);
	if (/\s/.test(query)) return null;
	return query;
}

/** A name hit always outranks a description hit (palette's TITLE_BONUS). */
const NAME_BONUS = 40;

/** Score one command against the query, or null when it doesn't match. */
export function matchCommand(query: string, command: CommandSummary): number | null {
	const name = fuzzyScore(query, command.name);
	if (name !== null) return name + NAME_BONUS;
	return fuzzyScore(query, command.description ?? "");
}

/** The matching commands, best first. An empty or weak query keeps discovery
 *  order (the daemon already sorts by name), so the menu reads as a stable
 *  list rather than a shuffle. */
export function rankCommands(
	commands: readonly CommandSummary[],
	query: string,
): CommandSummary[] {
	const needle = query.trim();
	if (needle === "") return [...commands];
	const scored: { command: CommandSummary; order: number; score: number }[] = [];
	commands.forEach((command, order) => {
		const score = matchCommand(needle, command);
		if (score !== null) scored.push({ command, order, score });
	});
	scored.sort((a, b) => b.score - a.score || a.order - b.order);
	return scored.map((s) => s.command);
}

/** The draft after choosing *command*: "/name " ready for arguments.
 *  The menu only ever shows while the draft is a bare slash query, so
 *  replacing the whole draft is the insert. */
export function insertCommand(command: CommandSummary): string {
	return `/${command.name} `;
}

// ── Skills in the menu (TD-4502) ──
// A skill rides the same "/" menu as a command — both are "/name" inserts,
// and for a skill the daemon expands the body on send. One menu, one
// trigger, one muscle memory.

/** One flattened row of the slash menu. Commands and skills differ in what
 *  happens on send, not in how they're chosen, so the row carries just
 *  what the markup and the insert need. */
export interface SlashEntry {
	kind: "command" | "skill";
	name: string;
	source: CommandSummary["source"];
	description: string | null;
}

/** Score one skill against the query, or null when it doesn't match.
 *  Name hits beat description hits; whenToUse is the last resort. */
export function matchSkill(query: string, skill: SkillSummary): number | null {
	const name = fuzzyScore(query, skill.name);
	if (name !== null) return name + NAME_BONUS;
	const rest = [skill.description, skill.when_to_use].filter(Boolean).join(" ");
	return fuzzyScore(query, rest);
}

/** The matching commands and skills, best first. An empty or weak query keeps
 *  discovery order (commands, then skills, each daemon-sorted by name);
 *  otherwise score wins and ties keep commands ahead of skills — same score
 *  means the user typed something equally like both names, and the command
 *  is the older habit. */
export function rankEntries(
	commands: readonly CommandSummary[],
	skills: readonly SkillSummary[],
	query: string,
): SlashEntry[] {
	const commandRows: SlashEntry[] = commands.map((c) => ({
		kind: "command",
		name: c.name,
		source: c.source,
		description: c.description ?? null,
	}));
	const skillRows: SlashEntry[] = skills.map((s) => ({
		kind: "skill",
		name: s.name,
		source: s.source,
		description: s.description ?? null,
	}));
	const needle = query.trim();
	if (needle === "") return [...commandRows, ...skillRows];
	const scored: { entry: SlashEntry; order: number; score: number }[] = [];
	commands.forEach((command, order) => {
		const score = matchCommand(needle, command);
		if (score !== null) scored.push({ entry: commandRows[order], order, score });
	});
	skills.forEach((skill, order) => {
		const score = matchSkill(needle, skill);
		if (score !== null)
			scored.push({ entry: skillRows[order], order: commands.length + order, score });
	});
	scored.sort((a, b) => b.score - a.score || a.order - b.order);
	return scored.map((s) => s.entry);
}

/** The draft after choosing any menu row: "/name " either way — for a skill
 *  the daemon expands the body on send, so arguments ride along exactly
 *  like a command's. */
export function insertSlash(entry: SlashEntry): string {
	return `/${entry.name} `;
}

/** Where ↑/↓ lands in the menu. palette's wrapper, re-exported so callers
 *  don't import two modules for one menu. */
export const nextSlashIndex = nextIndex;

const SOURCE_LABELS: Record<CommandSummary["source"], string> = {
	workspace: "workspace",
	user: "global",
	workspace_fallback: "workspace · .claude",
	user_fallback: "global · .claude",
};

/** Where a command came from, for the menu's quiet right-hand tag. */
export function sourceLabel(source: CommandSummary["source"]): string {
	return SOURCE_LABELS[source];
}

/** Same tag for any menu row — a skill says what it is first, because
 *  choosing one sends its body instead of registering a command. */
export function entrySourceLabel(entry: SlashEntry): string {
	return entry.kind === "skill"
		? `skill · ${SOURCE_LABELS[entry.source]}`
		: SOURCE_LABELS[entry.source];
}
