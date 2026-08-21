// Pure helpers for the project home's Memory column (TD-2601).

export interface MemoryFile {
	path: string;
	name: string;
	content: string;
}

/** Empty-state copy — points at the first distill, not a blank card. */
export function memoryEmptyCopy(): string {
	return "No memory yet. End a session to distill the first notes into this folder.";
}

/** Locality copy — this folder, this machine. Never "sync". */
export function memoryLocalCopy(): string {
	return "Local to this machine and this folder. Nothing is uploaded.";
}
