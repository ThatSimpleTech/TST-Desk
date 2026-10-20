// What a fresh chat's start screen says about its git worktree (TD-5126).
//
// Pure, so the copy is unit-tested without rendering. The daemon decides
// everything here (whether there is a worktree, how dirty the main checkout
// was, how setup went); this only words it.

import type { SessionWorktreeInfo } from "./protocol";

export interface WorktreeNotice {
	/** Always shown: where this chat works. */
	where: string;
	/** The main checkout's uncommitted changes, which the worktree lacks. */
	dirty: string | null;
	/** The setup command's state, when there is one to report. */
	setup: string | null;
	/** True when the setup failed or was skipped: shown in the warning tone. */
	setupWarn: boolean;
}

function plural(n: number, one: string, many: string): string {
	return n === 1 ? one : many;
}

export function worktreeNotice(wt: SessionWorktreeInfo): WorktreeNotice {
	const dirty =
		wt.main_dirty > 0
			? `Your main checkout has ${wt.main_dirty} uncommitted ${plural(wt.main_dirty, "change", "changes")}. ` +
				"This chat starts from the last commit, so it won't have " +
				`${plural(wt.main_dirty, "it", "them")}.`
			: null;
	let setup: string | null = null;
	if (wt.setup === "running") setup = "Setting up the worktree. Your first message waits for it.";
	else if (wt.setup === "failed") setup = `Worktree setup failed. ${wt.setup_note}`.trim();
	else if (wt.setup === "skipped") setup = wt.setup_note || "Worktree setup was skipped.";
	return {
		where: `Works in its own git worktree on ${wt.branch}.`,
		dirty,
		setup,
		setupWarn: wt.setup === "failed" || wt.setup === "skipped",
	};
}
