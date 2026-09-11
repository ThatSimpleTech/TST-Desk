// Inspector tab-strip counts.
//
// The Files and Work tabs carry the number of things behind them, so the
// strip answers "did the agent write anything?" without a click. Both
// figures come from the same fold the panes render from (files.ts), so a
// badge can never disagree with the list under it. Pure so the saturation
// rule and the fold are asserted without rendering the shell.

import { foldFileWrites } from "./files";
import type { TimelineEntry } from "./timeline";

export interface InspectorCounts {
	/** Distinct paths written — what the Files tab lists. */
	files: number;
	/** Individual writes — what the Work stack lists. */
	work: number;
}

export function inspectorCounts(entries: readonly TimelineEntry[]): InspectorCounts {
	const folded = foldFileWrites(entries);
	return { files: folded.fileCount, work: folded.writeCount };
}

/** A tab's count badge: null when there is nothing to count, saturating at
 *  99+ so a long session cannot widen the strip. */
export function tabCount(count: number): string | null {
	if (count <= 0) return null;
	return count > 99 ? "99+" : String(count);
}
