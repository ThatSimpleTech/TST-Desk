// Slash-command matching for the composer (TD-4501).
//
// Pure helpers so `/` behaviour is testable without mounting the card.
// The daemon is the source of names; this module only filters and inserts.

import type { CommandEntry, SkillStackEntry } from "./protocol";

/**
 * If the draft is a slash invocation at the start (optional leading
 * whitespace, then `/`, then a name with no spaces), return the query
 * after `/`. Otherwise null — the picker stays closed.
 */
export function slashQuery(draft: string): string | null {
	const match = draft.match(/^(\s*)\/(\S*)$/);
	if (match === null) return null;
	return match[2] ?? "";
}

/**
 * A palette row: a daemon command, a skill, or one the window handles
 * itself (TD-5109), which never reaches the model as text.
 */
export type SlashItem = Omit<CommandEntry, "source"> & {
	source: CommandEntry["source"] | "built-in";
};

/** Built-ins, listed after commands and skills of the same name. */
export const BUILTIN_SLASH_ITEMS: readonly SlashItem[] = [
	{
		name: "compact",
		description: "Summarize earlier turns now. Add words to say what to keep.",
		source: "built-in",
		body: "/compact ",
	},
	{
		name: "plan",
		description: "Plan mode: read-only research, then a plan you approve. /plan on, /plan off, or /plan <task>.",
		source: "built-in",
		body: "/plan ",
	},
];

/** Prefix-match command names. Case-insensitive. */
export function filterCommands<T extends SlashItem>(commands: readonly T[], query: string): T[] {
	const q = query.toLowerCase();
	return commands.filter((c) => c.name.toLowerCase().startsWith(q));
}

/** Inserted when the user picks a skill that is not also a command. */
export function skillLoadMarker(name: string): string {
	return `Load skill \`${name}\`.`;
}

/**
 * Slash palette items: commands first, then skills whose names are not
 * already a command (TD-4501 wins on the same stem), then built-ins whose
 * names are still free.
 */
export function mergeSlashItems(
	commands: readonly CommandEntry[],
	skills: readonly SkillStackEntry[],
	builtins: readonly SlashItem[] = [],
): SlashItem[] {
	const taken = new Set(commands.map((c) => c.name.toLowerCase()));
	const extras: CommandEntry[] = skills
		.filter((s) => !taken.has(s.name.toLowerCase()))
		.map((s) => ({
			name: s.name,
			description: s.description,
			source: s.source,
			body: skillLoadMarker(s.name),
		}));
	for (const extra of extras) taken.add(extra.name.toLowerCase());
	const own = builtins.filter((b) => !taken.has(b.name.toLowerCase()));
	return [...commands, ...extras, ...own];
}

/** Replace the `/name` token with the command body. Leading whitespace stays. */
export function insertCommandBody(draft: string, body: string): string {
	const match = draft.match(/^(\s*)\/(\S*)$/);
	if (match === null) return draft;
	return `${match[1] ?? ""}${body}`;
}
