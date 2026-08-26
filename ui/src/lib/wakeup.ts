// Wake-up summary helpers (TD-4303).
//
// The daemon owns the summary. This module only joins the workspace-relative
// ledger path the event named and fires the one-click pair: open the ledger
// in the OS editor and copy the auto-branch name. No second daemon.

import { joinUnderRoot } from "./artifacts";
import { openInEditor } from "./open-file";
import type { AutonomySummary } from "./protocol";

export type WakeupOpenDeps = {
	openFile?: (path: string) => Promise<boolean>;
	copyText?: (text: string) => Promise<boolean>;
};

/** Absolute ledger path from daemon fields, or null when it cannot be joined. */
export function ledgerAbsolutePath(
	workspacePath: string | null,
	ledgerPath: string,
): string | null {
	if (workspacePath === null || workspacePath === "") return null;
	return joinUnderRoot(workspacePath, ledgerPath);
}

/** One click: open the ledger and copy the branch name. */
export async function openBranchAndLedger(
	summary: Pick<AutonomySummary, "branch" | "ledger_path">,
	workspacePath: string | null,
	deps: WakeupOpenDeps = {},
): Promise<{ opened: boolean; copied: boolean }> {
	const openFile = deps.openFile ?? openInEditor;
	const copyText = deps.copyText ?? copyToClipboard;
	const ledger = ledgerAbsolutePath(workspacePath, summary.ledger_path);
	const opened = ledger !== null ? await openFile(ledger) : false;
	const copied = summary.branch !== "" ? await copyText(summary.branch) : false;
	return { opened, copied };
}

async function copyToClipboard(text: string): Promise<boolean> {
	try {
		if (typeof navigator === "undefined" || navigator.clipboard == null) {
			return false;
		}
		await navigator.clipboard.writeText(text);
		return true;
	} catch {
		return false;
	}
}
