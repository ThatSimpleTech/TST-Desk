// Scheduled rail store (TD-3805, TD-3810, TD-3811).
//
// Data-dir jobs from `job_list`. Create / edit / pause / delete / run send
// protocol verbs; the daemon talks to the store. Run now starts a turn.
// The tick still owns the schedule. Edit loads a row into the draft; the
// daemon keeps the run receipt because the save carries the existing id.
// History is a separate log: opening a disclosure asks for `list_job_runs`,
// and an open one is asked again when that job's `last_run` moves.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, JobEntry, JobRunEntry } from "./protocol";
import {
	draftFromJob,
	emptyDraft,
	saveFromDraft,
	saveFromEdit,
	saveFromJob,
	type JobDraftFields,
} from "./scheduled";

export const scheduled = $state({
	items: [] as JobEntry[],
	loading: false,
	error: null as string | null,
	draft: emptyDraft(null),
	parseText: "",
	editingId: null as string | null,
	/** Job ids whose History disclosure is open. */
	historyOpen: {} as Record<string, boolean>,
	/** `last_run` the open disclosure was last fetched against. Null is "never run". */
	historySeen: {} as Record<string, string | null>,
	/** Runs by job id, newest first. Absent until `job_runs` arrives. */
	runs: {} as Record<string, JobRunEntry[]>,
});

let started = false;
let stopEvents: (() => void) | null = null;
// A create or an edit-save is in flight. The daemon acks with `job_list`,
// which is the only signal that it took the draft — clearing the form
// before that would throw away the user's text on a validation error.
let savePending = false;

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
	savePending = false;
	stopEvents?.();
	stopEvents = null;
	scheduled.items = [];
	scheduled.loading = false;
	scheduled.error = null;
	scheduled.draft = emptyDraft(null);
	scheduled.parseText = "";
	scheduled.editingId = null;
	scheduled.historyOpen = {};
	scheduled.historySeen = {};
	scheduled.runs = {};
}

function freshDraft(): JobDraftFields {
	// Keep the workspace: the next job is usually in the same folder,
	// whether the last one was created or just edited.
	return emptyDraft(scheduled.draft.workspace || null);
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
	const sent = sendToDaemon(saveFromDraft(scheduled.draft));
	// Only arm the reset if the frame actually went out; a create that
	// never left must keep what the user typed.
	if (sent) savePending = true;
	return sent;
}

/** Load a row into the form. Run now, Pause and Delete keep working. */
export function editJob(jobId: string): boolean {
	const job = scheduled.items.find((row) => row.id === jobId);
	if (job === undefined) return false;
	ensureStarted();
	// A save already in flight belongs to the previous draft. Letting its
	// ack land must not wipe the row just loaded.
	savePending = false;
	scheduled.error = null;
	scheduled.editingId = job.id;
	scheduled.draft = draftFromJob(job);
	return true;
}

/** Leave edit mode and restore the empty new-job draft. */
export function cancelEdit(): void {
	if (scheduled.editingId === null) return;
	savePending = false;
	scheduled.editingId = null;
	scheduled.error = null;
	scheduled.draft = freshDraft();
}

/** Save the row in the form. The id is what keeps the receipt. */
export function saveEdit(): boolean {
	const id = scheduled.editingId;
	if (id === null) return false;
	const job = scheduled.items.find((row) => row.id === id);
	if (job === undefined) return false;
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	const sent = sendToDaemon(saveFromEdit(scheduled.draft, job));
	if (sent) savePending = true;
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
	// The form was showing this row. Drop it now; a failed delete leaves
	// the row in the list and the user can open it again.
	if (scheduled.editingId === jobId) {
		savePending = false;
		scheduled.editingId = null;
		scheduled.draft = freshDraft();
	}
	return sendToDaemon({ type: "delete_job", job_id: jobId });
}

/** Open or close one job's history. Opening asks for the log. */
export function toggleJobHistory(jobId: string): void {
	ensureStarted();
	const open = scheduled.historyOpen[jobId] !== true;
	scheduled.historyOpen = { ...scheduled.historyOpen, [jobId]: open };
	if (!open) return;
	const job = scheduled.items.find((row) => row.id === jobId);
	// Record the receipt we are fetching against before the reply, so a
	// job_list that still shows this last_run does not ask a second time.
	scheduled.historySeen = {
		...scheduled.historySeen,
		[jobId]: job?.last_run ?? null,
	};
	sendToDaemon({ type: "list_job_runs", job_id: jobId });
}

/** Ask the daemon to fire one job now. The schedule stays where it is. */
export function runJob(jobId: string): boolean {
	if (!scheduled.items.some((row) => row.id === jobId)) return false;
	ensureStarted();
	scheduled.loading = true;
	scheduled.error = null;
	return sendToDaemon({ type: "run_job", job_id: jobId });
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "job_list") {
		scheduled.items = event.jobs;
		scheduled.loading = false;
		scheduled.error = null;
		syncOpenHistory(event.jobs);
		if (savePending) {
			savePending = false;
			scheduled.editingId = null;
			scheduled.draft = freshDraft();
			return;
		}
		// The row being edited is gone (deleted elsewhere, or the ack raced
		// the click). The form must not keep offering Save for a missing id.
		if (
			scheduled.editingId !== null &&
			!event.jobs.some((row) => row.id === scheduled.editingId)
		) {
			scheduled.editingId = null;
			scheduled.draft = freshDraft();
		}
		return;
	}
	if (event.type === "job_runs") {
		if (!scheduled.items.some((row) => row.id === event.job_id)) return;
		scheduled.runs = { ...scheduled.runs, [event.job_id]: event.runs };
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
			// A sentence does not name a model. Keep the pin already on the form.
			preset: scheduled.draft.preset,
			engine: scheduled.draft.engine,
		};
		return;
	}
	if (
		event.type === "error" &&
		(event.code === "job_invalid" || event.code === "job_not_found" || event.code === "job_running")
	) {
		savePending = false;
		scheduled.loading = false;
		scheduled.error = event.message;
	}
}

/** Keep open disclosures, and ask again when that job's receipt moved. */
function syncOpenHistory(jobs: JobEntry[]): void {
	const live = new Set(jobs.map((job) => job.id));
	const open: Record<string, boolean> = {};
	const seen: Record<string, string | null> = {};
	const runs: Record<string, JobRunEntry[]> = {};
	for (const [id, rows] of Object.entries(scheduled.runs)) {
		if (live.has(id)) runs[id] = rows;
	}
	for (const job of jobs) {
		if (scheduled.historyOpen[job.id] !== true) continue;
		open[job.id] = true;
		const current = job.last_run ?? null;
		const had = Object.prototype.hasOwnProperty.call(scheduled.historySeen, job.id);
		if (had && (scheduled.historySeen[job.id] ?? null) !== current) {
			sendToDaemon({ type: "list_job_runs", job_id: job.id });
		}
		seen[job.id] = current;
	}
	scheduled.historyOpen = open;
	scheduled.historySeen = seen;
	scheduled.runs = runs;
}
