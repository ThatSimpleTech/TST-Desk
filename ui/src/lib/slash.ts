// Slash-command matching for the composer (TD-4501).
//
// Pure and rune-free like palette.ts: the trigger test, the ranking, and the
// insertion shape are unit-testable in the node vitest environment, while
// commands-store.svelte.ts holds the fetched listing and the menu state.
//
// Ranking reuses the palette's subsequence matcher — same list size, same
// feel, one matcher to reason about.

import { fuzzyScore, nextIndex } from "./palette";
import type { CommandSummary } from "./protocol";

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
