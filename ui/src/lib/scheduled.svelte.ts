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
});

let started = false;
let stopEvents: (() => void) | null = null;
// A create is in flight. The daemon acks with `job_list`, which is the
// only signal that it took the draft — clearing the form before that
// would throw away the user's text on a validation error.
let createPending = false;

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
	createPending = false;
	stopEvents?.();
	stopEvents = null;
	scheduled.items = [];
	scheduled.loading = false;
	scheduled.error = null;
	scheduled.draft = emptyDraft(null);
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

export function createJob(): boolean {
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	const sent = sendToDaemon(saveFromDraft(scheduled.draft));
	// Only arm the reset if the frame actually went out; a create that
	// never left must keep what the user typed.
	if (sent) createPending = true;
	return sent;
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
		if (createPending) {
			createPending = false;
			// Keep the workspace — the next job is usually in the same one.
			scheduled.draft = emptyDraft(scheduled.draft.workspace || null);
		}
		return;
	}
	if (event.type === "error" && (event.code === "job_invalid" || event.code === "job_not_found")) {
		createPending = false;
		scheduled.loading = false;
		scheduled.error = event.message;
	}
}
