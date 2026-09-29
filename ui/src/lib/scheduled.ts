// Scheduled rail helpers (TD-3805).
//
// Draft fields the pane sends on `save_job` — not a natural-language
// parse. Pause is the same verb with `paused` flipped. Run now is
// `run_job`; the tick still owns the schedule.

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
		cadence: "weekdays at 9:00",
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

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DAY_PLURALS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

/** Cron day-of-week field as a set of 0-6 (7 is Sunday), or null if not a plain list/range. */
function parseDays(field: string): Set<number> | null {
	const days = new Set<number>();
	for (const part of field.split(",")) {
		const m = /^(\d)(?:-(\d))?$/.exec(part);
		if (m === null) return null;
		const lo = Number(m[1]);
		const hi = m[2] === undefined ? lo : Number(m[2]);
		if (lo > hi || hi > 7) return null;
		for (let d = lo; d <= hi; d += 1) days.add(d % 7);
	}
	return days;
}

function clock(hour: number, minute: number): string {
	const suffix = hour < 12 ? "AM" : "PM";
	return `${hour % 12 === 0 ? 12 : hour % 12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

/**
 * A stored cadence in words: `45 7 * * 1-5` becomes "Weekdays at 7:45 AM".
 *
 * Deliberately narrow — fixed minute and hour, dom/month `*`, dow a list or
 * range. Anything else (steps, names, `*` hours, a plain-English cadence the
 * daemon kept as written) comes back unchanged, because a wrong translation
 * of a schedule is worse than the raw cron. Legacy jobs with no zone run in
 * UTC, so the clock time says so.
 */
export function humanizeCadence(cadence: string, timezone?: string | null): string {
	const f = cadence.trim().split(/\s+/);
	if (f.length !== 5 || f[2] !== "*" || f[3] !== "*") return cadence;
	if (!/^\d{1,2}$/.test(f[0]) || !/^\d{1,2}$/.test(f[1])) return cadence;
	const minute = Number(f[0]);
	const hour = Number(f[1]);
	if (minute > 59 || hour > 23) return cadence;
	const days = f[4] === "*" ? null : parseDays(f[4]);
	if (f[4] !== "*" && (days === null || days.size === 0)) return cadence;
	const key = days === null ? "" : [...days].sort((a, b) => a - b).join("");
	let who: string;
	if (days === null || days.size === 7) who = "Daily";
	else if (key === "12345") who = "Weekdays";
	else if (key === "06") who = "Weekends";
	else if (days.size === 1) who = DAY_PLURALS[[...days][0]];
	else {
		const names = [...days].sort((a, b) => ((a + 6) % 7) - ((b + 6) % 7)).map((d) => DAY_NAMES[d]);
		who = `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
	}
	const utc = timezone ? "" : " UTC";
	return `${who} at ${clock(hour, minute)}${utc}`;
}

/** Known project folders for the workspace field: pinned first, then recents, deduped. */
export function workspaceSuggestions(pinned: string[], recents: string[]): string[] {
	return [...new Set([...pinned, ...recents])];
}

/** The browser's own zone, or undefined when the runtime cannot say. */
export function viewerTimeZone(): string | undefined {
	try {
		return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
	} catch {
		return undefined;
	}
}

/** Row meta: when it next fires, the cadence in words if that adds anything, and where it delivers. */
export function jobMeta(job: JobEntry, timeZone?: string): string {
	const when = jobWhen(job, timeZone);
	const words = job.cadence ? humanizeCadence(job.cadence, job.timezone) : null;
	// With no next run, jobWhen already fell back to the raw cadence; show
	// the readable form instead of both.
	const parts = words !== null && when === job.cadence ? [words] : [when];
	if (words !== null && when !== job.cadence) parts.push(words);
	parts.push(job.deliver_to);
	return parts.join(" · ");
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

/** Row status. An in-flight turn replaces the previous receipt until it lands. */
export function jobActivity(job: JobEntry, timeZone?: string): string {
	if (job.running) return "Running…";
	return jobLastRun(job, timeZone);
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
export function saveFromDraft(
	draft: JobDraftFields,
	timezone: string | undefined = viewerTimeZone(),
): SaveJob {
	return {
		type: "save_job",
		workspace: blankToNull(draft.workspace),
		instruction: blankToNull(draft.instruction),
		cadence: blankToNull(draft.cadence),
		next_run: blankToNull(draft.next_run),
		deliver_to: draft.deliver_to,
		paused: draft.paused,
		// The zone the user typed the cadence in; without it the daemon
		// would read "7:45" as UTC.
		timezone,
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
		timezone: job.timezone,
	};
}
