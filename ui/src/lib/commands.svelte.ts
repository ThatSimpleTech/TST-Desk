// Slash-command listing (TD-4501).
//
// The composer's "/" menu reads this. The daemon answers list_commands
// with a connection-scoped commands event — nothing pushes it and it is
// never replayed on attach — so the menu re-asks each time it opens
// rather than trusting a list that may predate an edit to a command file.
//
// Wiring mirrors settings.svelte.ts: listens on the connection fan-out,
// sends only via sendToDaemon.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { CommandEntry, DaemonEventUnion } from "./protocol";

export const slashCommands = $state({
  /** Commands for the open workspace, from the most recent listing. */
  items: [] as CommandEntry[],
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
  started = false;
}

function reduce(event: DaemonEventUnion): void {
  if (event.type === "commands") {
    slashCommands.items = event.commands;
  }
}

/** Ask for a workspace's commands. With no workspace open there is
 * nothing to ask about — clear the list instead. */
export function requestCommands(workspacePath: string | null): void {
  if (workspacePath === null) {
    slashCommands.items = [];
    return;
  }
  sendToDaemon({ type: "list_commands", workspace_path: workspacePath });
}

/** What still matches after the leading "/" — prefix, case-insensitive. */
export function matchCommands(
  items: readonly CommandEntry[],
  query: string,
): CommandEntry[] {
  const q = query.trim().toLowerCase();
  if (q === "") return [...items];
  return items.filter((c) => c.name.toLowerCase().startsWith(q));
}
