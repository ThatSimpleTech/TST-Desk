import type { ContextPinEntry } from "./protocol";

export type ContextPin = ContextPinEntry;

/** Client-side search. Does not fetch the web. */
export function filterPins(pins: readonly ContextPin[], query: string): ContextPin[] {
	const q = query.trim().toLowerCase();
	if (q === "") return [...pins];
	return pins.filter((p) => p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q));
}

export function contextEmptyCopy(): string {
	return "Pin files or folders from this workspace. They stay on disk.";
}

export function pinKindLabel(kind: ContextPin["kind"]): string {
	return kind === "dir" ? "folder" : "file";
}
