// Pure helpers for the project list and project home (TD-2801 / TD-2807).
//
// The rail's Projects surface is a list of known workspaces and a home
// per folder: name, New chat, recents filtered to that path. Recents
// are session_list rows for this folder, newest first. Pin persistence
// is TD-2806; this module only names the list.

export interface ProjectSession {
	sessionId: string;
	workspacePath: string;
	archived: boolean;
	updatedAt: string;
}

/** Recents on a project home: this folder, newest first.
 *
 *  Archived rows stay off the default list. Pass ``archived: true``
 *  when the Archived filter is on (same bit as the rail).
 */
export function projectSessions<T extends ProjectSession>(
	rows: readonly T[],
	workspacePath: string,
	opts: { archived?: boolean } = {},
): T[] {
	const wantArchived = opts.archived === true;
	return rows
		.filter((r) => r.workspacePath === workspacePath && r.archived === wantArchived)
		.slice()
		.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0));
}

/** Empty-state copy for the project list. */
export function projectListEmptyCopy(): string {
	return "Open a folder from the title bar to start a project.";
}

/** Empty-state copy for a project home with no chats on this shelf. */
export function projectRecentsEmptyCopy(archived = false): string {
	return archived ? "No archived chats in this project." : "No chats in this project yet.";
}
