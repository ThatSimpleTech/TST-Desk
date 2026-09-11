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

/**
 * A stored UTC instant as local wall-clock time.
 *
 * Cadences and `next_run` are stored and evaluated in UTC, so showing the
 * raw string told a user in any other zone the wrong hour. `timeZone` is
 * for tests; leaving it undefined uses the viewer's own zone, which is the
 * whole point.  An unparseable value is shown as-is rather than as
 * "Invalid Date".
 */
export function formatLocal(iso: string, timeZone?: string): string {
	const when = new Date(iso);
	if (Number.isNaN(when.getTime())) return iso;
	try {
		return new Intl.DateTimeFormat(undefined, {
			dateStyle: "medium",
			timeStyle: "short",
			timeZone,
		}).format(when);
	} catch {
		return iso;
	}
}

export function jobWhen(job: JobEntry, timeZone?: string): string {
	if (job.paused) return "Paused";
	if (job.next_run) return formatLocal(job.next_run, timeZone);
	if (job.cadence) return job.cadence;
	return "Unscheduled";
}

/**
 * The last fire, as a line the rail can show (TD-3807).
 *
 * "Never run" is the honest answer for a job that has not fired yet, and is
 * the one a user most needs when a schedule looks wrong.
 */
export function jobLastRun(job: JobEntry, timeZone?: string): string {
	if (!job.last_run) return "Never run";
	const when = formatLocal(job.last_run, timeZone);
	return job.last_status === "failed" ? `Failed ${when}` : `Ran ${when}`;
}

/** True when the last fire failed, so the row can mark itself. */
export function jobFailed(job: JobEntry): boolean {
	return job.last_status === "failed";
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
