// Pure helpers for the project list and project home (TD-2801).
//
// The rail's Projects surface is a list of known workspaces and a home
// per folder: name, New chat, recents filtered to that path. Pin
// persistence is TD-2806; this module only names the list.

export interface ProjectSession {
	sessionId: string;
	workspacePath: string;
	archived: boolean;
}

/** Recents on a project home: this folder, not archived, caller order. */
export function projectSessions<T extends ProjectSession>(
	rows: readonly T[],
	workspacePath: string,
): T[] {
	return rows.filter((r) => r.workspacePath === workspacePath && !r.archived);
}

/** Empty-state copy for the project list. */
export function projectListEmptyCopy(): string {
	return "Open a folder from the title bar to start a project.";
}

/** Empty-state copy for a project home with no chats. */
export function projectRecentsEmptyCopy(): string {
	return "No chats in this project yet.";
}
