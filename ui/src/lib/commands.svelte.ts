// Slash-command and skill listings (TD-4501, TD-4502).
//
// The composer's "/" menu reads this. The daemon answers list_commands
// and list_skills with connection-scoped commands/skills events — nothing
// pushes them and they are never replayed on attach — so the menu re-asks
// each time it opens rather than trusting a list that may predate an edit
// to a command file.
//
// Wiring mirrors settings.svelte.ts: listens on the connection fan-out,
// sends only via sendToDaemon.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { CommandEntry, DaemonEventUnion, SkillSummary } from "./protocol";

export const slashCommands = $state({
  /** Commands for the open workspace, from the most recent listing. */
  items: [] as CommandEntry[],
  /** Skills for the open workspace — same menu, body sent on invoke. */
  skills: [] as SkillSummary[],
});

let started = false;

/** Register the reducer. Unsubscribe for tests. */
export function startSlashCommands(): () => void {
  if (started) return () => {};
  started = true;
  return onEvent(reduce);
}

/** Reset for tests. */
export function resetSlashCommands(): void {
  slashCommands.items = [];
  slashCommands.skills = [];
  started = false;
}

function reduce(event: DaemonEventUnion): void {
  if (event.type === "commands") {
    slashCommands.items = event.commands;
  } else if (event.type === "skills") {
    slashCommands.skills = event.skills;
  }
}

/** Ask for a workspace's command and skill listings — one menu draws from
 * both, so one ask covers them. With no workspace open there is nothing to
 * ask about — clear the lists instead. */
export function requestCommands(workspacePath: string | null): void {
  if (workspacePath === null) {
    slashCommands.items = [];
    slashCommands.skills = [];
    return;
  }
  sendToDaemon({ type: "list_commands", workspace_path: workspacePath });
  sendToDaemon({ type: "list_skills", workspace_path: workspacePath });
}

/** What still matches after the leading "/" — prefix, case-insensitive.
 * Shared by commands and skills below; one matcher, one menu feel. */
function prefixMatches<T extends { name: string }>(items: readonly T[], query: string): T[] {
  const q = query.trim().toLowerCase();
  if (q === "") return [...items];
  return items.filter((c) => c.name.toLowerCase().startsWith(q));
}

/** What still matches after the leading "/" — prefix, case-insensitive. */
export function matchCommands(
  items: readonly CommandEntry[],
  query: string,
): CommandEntry[] {
  return prefixMatches(items, query);
}

/** Same filter over the skill catalog (TD-4502). */
export function matchSkills(
  items: readonly SkillSummary[],
  query: string,
): SkillSummary[] {
  return prefixMatches(items, query);
}
