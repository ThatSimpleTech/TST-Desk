// Per-file fold of the session's write diffs (TD-1705).
//
// The Files pane is aggregation, not protocol. Every successful write already
// reports what it changed on the `tool_result` event's `diff` field (TD-604),
// and timeline.ts carries that onto the entry — so this folds the diffs the
// stream already delivered by path. Pure and DOM-free so it unit-tests under
// vitest's node environment; FilesPanel.svelte is the view over it.
//
// A file's path comes from the diff's own `+++ b/<path>` header, never from
// the tool call's arguments. The header holds the canonical absolute path the
// daemon actually wrote — an argument may be relative, and one call can carry
// two write targets — so the header is the only place the truth is stated
// (§6: the UI never derives truth it wasn't given).

import { classifyDiffLine, diffLines } from "./entry-view";
import type { TimelineEntry } from "./timeline";

/** The `--- a/` / `+++ b/` pair the daemon renders at the head of a section. */
const OLD_HEADER = "--- a/";
const NEW_HEADER = "+++ b/";

/** One file's slice of a single tool result's diff. */
export interface DiffSection {
	/** Absolute path, taken from the `+++ b/` header. */
	path: string;
	/** The section verbatim, headers included, ready to render. */
	diff: string;
	added: number;
	removed: number;
}

/** One write that touched a file, in stream order. */
export interface FileWrite {
	/** Event seq of the `tool_result` that carried this diff. */
	seq: number;
	/** The tool that wrote, when its `tool_call` was seen; null otherwise. */
	tool: string | null;
	diff: string;
	added: number;
	removed: number;
}

/** Everything the session did to one path. */
export interface FileChange {
	path: string;
	/** Oldest first — the order the writes landed. */
	writes: FileWrite[];
	added: number;
	removed: number;
	/** Seq of the most recent write; drives the list order. */
	lastSeq: number;
}

/** The pane's whole model: the file list plus the running totals. */
export interface FilesSummary {
	/** Most recently written first, so a live session reads top-down. */
	files: FileChange[];
	fileCount: number;
	writeCount: number;
	added: number;
	removed: number;
}

/** Everything after the last `/`, or the whole path when it has none. */
export function baseName(path: string): string {
	const at = path.lastIndexOf("/");
	return at < 0 ? path : path.slice(at + 1);
}

/** Everything up to the last `/`, or "" when the path has none. */
export function dirName(path: string): string {
	const at = path.lastIndexOf("/");
	return at < 0 ? "" : path.slice(0, at);
}

/** Trim the blank separator lines a section collects from the join. */
function sectionText(lines: string[]): string {
	const out = [...lines];
	while (out.length > 0 && out[out.length - 1] === "") out.pop();
	return out.join("\n");
}

/** Split one tool result's diff into its per-file sections.
 *
 *  A tool with two write targets (a move) reports both, joined by a blank
 *  line. That blank line is also what separates a real header from a removed
 *  line that happens to look like one, so it is part of the header test. */
export function parseDiffSections(diff: string): DiffSection[] {
	const lines = diffLines(diff);
	const sections: DiffSection[] = [];
	let path: string | null = null;
	let body: string[] = [];
	let added = 0;
	let removed = 0;

	function flush(): void {
		if (path === null) return;
		sections.push({ path, diff: sectionText(body), added, removed });
	}

	for (let i = 0; i < lines.length; i++) {
		const line = lines[i];
		const next = lines[i + 1] ?? "";
		const atHeader =
			line.startsWith(OLD_HEADER) &&
			next.startsWith(NEW_HEADER) &&
			(i === 0 || lines[i - 1] === "");
		if (atHeader) {
			flush();
			path = next.slice(NEW_HEADER.length);
			body = [line, next];
			added = 0;
			removed = 0;
			i++;
			continue;
		}
		// Anything before the first header belongs to no file; dropping it
		// beats attributing it to one.
		if (path === null) continue;
		body.push(line);
		const kind = classifyDiffLine(line);
		if (kind === "add") added++;
		else if (kind === "del") removed++;
	}
	flush();
	return sections;
}

/** Fold the timeline's write diffs into the Files pane's model.
 *
 *  Only successful writes carry a diff, so no status filter is needed: the
 *  presence of the field is the daemon saying a write landed. */
export function foldFileWrites(entries: readonly TimelineEntry[]): FilesSummary {
	// tool_result carries no tool name, so the name comes from the call it
	// answers, matched on tool_call_id.
	const toolOf = new Map<string, string>();
	const byPath = new Map<string, FileChange>();
	let writeCount = 0;

	for (const entry of entries) {
		if (entry.kind === "tool_call") {
			if (entry.toolCallId !== undefined) toolOf.set(entry.toolCallId, entry.title);
			continue;
		}
		if (entry.kind !== "tool_result") continue;
		const diff = entry.details.diff;
		if (typeof diff !== "string" || diff === "") continue;
		const tool = entry.toolCallId === undefined ? null : (toolOf.get(entry.toolCallId) ?? null);

		for (const section of parseDiffSections(diff)) {
			writeCount++;
			const write: FileWrite = {
				seq: entry.seq,
				tool,
				diff: section.diff,
				added: section.added,
				removed: section.removed,
			};
			const existing = byPath.get(section.path);
			if (existing === undefined) {
				byPath.set(section.path, {
					path: section.path,
					writes: [write],
					added: section.added,
					removed: section.removed,
					lastSeq: entry.seq,
				});
			} else {
				existing.writes.push(write);
				existing.added += section.added;
				existing.removed += section.removed;
				existing.lastSeq = entry.seq;
			}
		}
	}

	const files = [...byPath.values()].sort((a, b) => b.lastSeq - a.lastSeq);
	return {
		files,
		fileCount: files.length,
		writeCount,
		added: files.reduce((sum, f) => sum + f.added, 0),
		removed: files.reduce((sum, f) => sum + f.removed, 0),
	};
}
