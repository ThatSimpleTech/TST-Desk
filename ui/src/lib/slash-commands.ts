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

/** Prefix-match command names. Case-insensitive. */
export function filterCommands(commands: readonly CommandEntry[], query: string): CommandEntry[] {
	const q = query.toLowerCase();
	return commands.filter((c) => c.name.toLowerCase().startsWith(q));
}

/** Inserted when the user picks a skill that is not also a command. */
export function skillLoadMarker(name: string): string {
	return `Load skill \`${name}\`.`;
}

/**
 * Slash palette items: commands first, then skills whose names are not
 * already a command (TD-4501 wins on the same stem).
 */
export function mergeSlashItems(
	commands: readonly CommandEntry[],
	skills: readonly SkillStackEntry[],
): CommandEntry[] {
	const taken = new Set(commands.map((c) => c.name.toLowerCase()));
	const extras: CommandEntry[] = skills
		.filter((s) => !taken.has(s.name.toLowerCase()))
		.map((s) => ({
			name: s.name,
			description: s.description,
			source: s.source,
			body: skillLoadMarker(s.name),
		}));
	return [...commands, ...extras];
}

/** Replace the `/name` token with the command body. Leading whitespace stays. */
export function insertCommandBody(draft: string, body: string): string {
	const match = draft.match(/^(\s*)\/(\S*)$/);
	if (match === null) return draft;
	return `${match[1] ?? ""}${body}`;
}
