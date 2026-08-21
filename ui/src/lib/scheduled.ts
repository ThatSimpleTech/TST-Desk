// Scheduled rail helpers (TD-3805).
//
// Draft fields the pane sends on `save_job` — not a natural-language
// parse. Pause is the same verb with `paused` flipped. The rail never
// runs a job; TD-3804's tick does that.

import type { JobEntry, SaveJob } from "./protocol";

export type DeliverTo = JobEntry["deliver_to"];

export interface JobDraftFields {
	workspace: string;
	instruction: string;
	cadence: string;
	next_run: string;
	deliver_to: DeliverTo;
	paused: boolean;
}

export function jobsEmptyCopy(): string {
	return "No scheduled jobs yet.";
}

export function emptyDraft(workspace: string | null): JobDraftFields {
	return {
		workspace: workspace ?? "",
		instruction: "",
		cadence: "every 1 hour",
		next_run: "",
		deliver_to: "window",
		paused: false,
	};
}

export function jobWhen(job: JobEntry): string {
	if (job.paused) return "Paused";
	if (job.next_run) return job.next_run;
	if (job.cadence) return job.cadence;
	return "Unscheduled";
}

function blankToNull(value: string): string | undefined {
	const text = value.trim();
	return text === "" ? undefined : text;
}

/** Wire payload for a new job. Empty cadence / next_run are omitted. */
export function saveFromDraft(draft: JobDraftFields): SaveJob {
	return {
		type: "save_job",
		workspace: blankToNull(draft.workspace),
		instruction: blankToNull(draft.instruction),
		cadence: blankToNull(draft.cadence),
		next_run: blankToNull(draft.next_run),
		deliver_to: draft.deliver_to,
		paused: draft.paused,
	};
}

/** Wire payload to replace a listed job, including pause. */
export function saveFromJob(job: JobEntry, paused: boolean): SaveJob {
	return {
		type: "save_job",
		id: job.id,
		workspace: job.workspace,
		instruction: job.instruction,
		cadence: job.cadence,
		next_run: job.next_run,
		deliver_to: job.deliver_to,
		paused,
	};
}
