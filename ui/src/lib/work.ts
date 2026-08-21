// Session write stack for the Work pane (TD-3203).
//
// Files (TD-1705) folds the same stream by path. Work is that fold flattened
// back into a reviewable stack: one row per write, not one row per file.
// Newest first — a live session reads top-down, same as the Files list.
// Pure so it unit-tests under vitest's node environment.

import { foldFileWrites } from "./files";
import type { TimelineEntry } from "./timeline";

/** One write in the Work stack — a single section of a single tool result. */
export interface WorkWrite {
	/** Stable expand key. seq is not unique on a two-target write. */
	id: string;
	path: string;
	seq: number;
	tool: string | null;
	diff: string;
	added: number;
	removed: number;
}

export interface WorkStack {
	/** Newest write first. */
	writes: WorkWrite[];
	writeCount: number;
	added: number;
	removed: number;
}

/** Empty-state copy. Distinct from the Files pane so the two are not clones. */
export const WORK_EMPTY_COPY =
	"This stack fills as the agent writes. Each write lands here to review — " +
	"path, line counts, and a click to open it in your editor.";

/** Flatten the Files fold into a write stack, newest first. */
export function stackSessionWrites(entries: readonly TimelineEntry[]): WorkStack {
	const folded = foldFileWrites(entries);
	const writes: WorkWrite[] = [];
	for (const file of folded.files) {
		file.writes.forEach((write, i) => {
			writes.push({
				id: `${write.seq}:${file.path}:${i}`,
				path: file.path,
				seq: write.seq,
				tool: write.tool,
				diff: write.diff,
				added: write.added,
				removed: write.removed,
			});
		});
	}
	writes.sort((a, b) => b.seq - a.seq || a.path.localeCompare(b.path));
	return {
		writes,
		writeCount: folded.writeCount,
		added: folded.added,
		removed: folded.removed,
	};
}
