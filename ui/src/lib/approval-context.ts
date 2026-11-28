// Which chat an approval is for, said on the card (JOURNEY2-10).
//
// The approval bar showed the bound chat's card on every surface, naming
// no chat. On Scheduled, beside a job reading "Waiting for approval · Run
// `npm test`", its Approve approved a force-push in another chat. A card
// now says whose it is, and off the chat surface the bar is a named chip
// that opens the chat instead of live Approve and Deny buttons.

export interface ChatRowLike {
  sessionId: string;
  title: string | null;
  workspacePath: string;
}

function folderName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** "trip notes · notes-repo", or the short id and folder when untitled. */
export function chatLabel(sessionId: string, rows: readonly ChatRowLike[]): string {
  const row = rows.find((r) => r.sessionId === sessionId);
  const title = row?.title?.trim() || `Chat ${sessionId.slice(0, 8)}`;
  return row ? `${title} · ${folderName(row.workspacePath)}` : title;
}

/** The bar shows live cards only where the chat itself is on screen. */
export function chatInView(surface: string): boolean {
  return surface === "home";
}
