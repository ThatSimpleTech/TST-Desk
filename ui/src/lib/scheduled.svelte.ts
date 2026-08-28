// Scheduled rail store (TD-3805).
//
// Data-dir jobs from `job_list`. Create / pause / delete send protocol
// verbs; the daemon talks to the store. This pane never starts a turn.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, JobEntry } from "./protocol";
import { emptyDraft, saveFromDraft, saveFromJob, type JobDraftFields } from "./scheduled";

export const scheduled = $state({
	items: [] as JobEntry[],
	loading: false,
	error: null as string | null,
	draft: emptyDraft(null),
	parseText: "",
});

let started = false;
let stopEvents: (() => void) | null = null;

function ensureStarted(): void {
	if (started) return;
	started = true;
	stopEvents = onEvent(reduce);
}

export function startScheduled(): () => void {
	ensureStarted();
	return () => {
		started = false;
		stopEvents?.();
		stopEvents = null;
	};
}

export function resetScheduled(): void {
	started = false;
	stopEvents?.();
	stopEvents = null;
	scheduled.items = [];
	scheduled.loading = false;
	scheduled.error = null;
	scheduled.draft = emptyDraft(null);
	scheduled.parseText = "";
}

/** Ask the daemon for the job list. Prefills workspace when the draft is empty. */
export function refreshJobs(workspaceHint?: string | null): void {
	ensureStarted();
	if (workspaceHint && scheduled.draft.workspace === "") {
		scheduled.draft = { ...scheduled.draft, workspace: workspaceHint };
	}
	scheduled.loading = true;
	scheduled.error = null;
	sendToDaemon({ type: "list_jobs" });
}

export function setDraftField<K extends keyof JobDraftFields>(
	key: K,
	value: JobDraftFields[K],
): void {
	scheduled.draft = { ...scheduled.draft, [key]: value };
}

export function setParseText(value: string): void {
	scheduled.parseText = value;
}

/** Ask the daemon to fill the draft from NL. Does not save. */
export function parseJobRequest(): boolean {
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	return sendToDaemon({ type: "parse_job", text: scheduled.parseText });
}

export function createJob(): boolean {
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	return sendToDaemon(saveFromDraft(scheduled.draft));
}

export function pauseJob(jobId: string): boolean {
	const job = scheduled.items.find((row) => row.id === jobId);
	if (job === undefined) return false;
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	return sendToDaemon(saveFromJob(job, !job.paused));
}

export function deleteScheduledJob(jobId: string): boolean {
	if (!scheduled.items.some((row) => row.id === jobId)) return false;
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	return sendToDaemon({ type: "delete_job", job_id: jobId });
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "job_list") {
		scheduled.items = event.jobs;
		scheduled.loading = false;
		scheduled.error = null;
		return;
	}
	if (event.type === "job_draft") {
		scheduled.loading = false;
		if (!event.ok) {
			scheduled.error = event.detail ?? "could not parse job request";
			return;
		}
		scheduled.error = null;
		scheduled.draft = {
			workspace: event.workspace ?? scheduled.draft.workspace,
			instruction: event.instruction ?? scheduled.draft.instruction,
			cadence: event.cadence ?? scheduled.draft.cadence,
			next_run: event.next_run ?? scheduled.draft.next_run,
			deliver_to: event.deliver_to ?? scheduled.draft.deliver_to,
			paused: event.paused ?? scheduled.draft.paused,
		};
		return;
	}
	if (event.type === "error" && (event.code === "job_invalid" || event.code === "job_not_found")) {
		scheduled.loading = false;
		scheduled.error = event.message;
	}
}
