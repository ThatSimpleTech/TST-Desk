// Scheduled rail helpers (TD-3805, TD-3810, TD-3811, TD-3812, TD-3813, TD-3814, TD-3817, TD-3818).
//
// Draft fields the pane sends on `save_job` — not a natural-language
// parse. Pause is the same verb with `paused` flipped. Edit is the same
// verb with the job's id. Run now is `run_job`; the tick still owns the
// schedule.

import type { JobEntry, JobRunEntry, SaveJob } from "./protocol";
import { durationPhrase, maxRunDraftValue } from "./scheduled-max-run";

export type DeliverTo = JobEntry["deliver_to"];

export interface JobDraftFields {
	workspace: string;
	instruction: string;
	cadence: string;
	next_run: string;
	deliver_to: DeliverTo;
	/** One address when deliver_to is email. `""` is none. */
	email_to: string;
	paused: boolean;
	/** Catalog preset name. `""` means use whatever the window is using. */
	preset: string;
	/** `""` means use the window's engine. */
	engine: "" | "native" | "grok";
	/** Phrase sent as `grace`, or `""` for always run. */
	grace: string;
	/** `""` is no retries. `"1"` `"2"` `"3"` are extra tries after the first. */
	retries: string;
	/** Another job's id, or `""` for no follow-on (TD-3817). */
	then: string;
	/** Absolute path of a local .ics file, or `""` for none (TD-3818). */
	skip_calendar: string;
	/** Substrings joined by `|`. `""` matches every event. */
	skip_match: string;
	/** Phrase sent as `max_run`, or `""` for the configured limit (TD-3819). */
	max_run: string;
}

/** If late. `seconds` is what the daemon stores; the wire value is the phrase. */
export const GRACE_CHOICES: readonly { value: string; label: string; seconds: number | null }[] = [
	{ value: "", label: "Always run", seconds: null },
	{ value: "30 minutes", label: "Skip if more than 30 min late", seconds: 30 * 60 },
	{ value: "1 hour", label: "Skip if more than 1 h late", seconds: 60 * 60 },
	{ value: "2 hours", label: "Skip if more than 2 h late", seconds: 2 * 60 * 60 },
	{ value: "6 hours", label: "Skip if more than 6 h late", seconds: 6 * 60 * 60 },
];

/** Extra tries. The delay is always 10 minutes; the daemon stores the seconds. */
export const RETRY_CHOICES: readonly { value: string; label: string }[] = [
	{ value: "", label: "None" },
	{ value: "1", label: "1" },
	{ value: "2", label: "2" },
	{ value: "3", label: "3" },
];

export const RETRY_DELAY = "10 minutes";

/** Draft value for a stored retry count. An unknown count still round-trips. */
export function retriesDraftValue(count: number | null | undefined): string {
	if (count == null || count <= 0) return "";
	return String(count);
}

/** Draft value for a stored grace. Unknown counts still round-trip on Save. */
export function graceDraftValue(seconds: number | null | undefined): string {
	if (seconds == null) return "";
	const known = GRACE_CHOICES.find((choice) => choice.seconds === seconds);
	if (known !== undefined) return known.value;
	return durationPhrase(seconds);
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
		email_to: "",
		paused: false,
		preset: "",
		engine: "",
		grace: "",
		retries: "",
		then: "",
		skip_calendar: "",
		skip_match: "",
		max_run: "",
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

/** Row meta: when it next fires, the cadence in words if that adds anything, where it delivers, and the job that runs after a success. */
export function jobMeta(job: JobEntry, timeZone?: string, jobs?: readonly JobEntry[]): string {
	const when = jobWhen(job, timeZone);
	const words = job.cadence ? humanizeCadence(job.cadence, job.timezone) : null;
	// With no next run, jobWhen already fell back to the raw cadence; show
	// the readable form instead of both.
	const parts = words !== null && when === job.cadence ? [words] : [when];
	if (words !== null && when !== job.cadence) parts.push(words);
	parts.push(job.deliver_to);
	if (job.preset) parts.push(job.preset);
	if (job.engine) parts.push(job.engine);
	// The row stores an id. The instruction is on the list the pane already has.
	if (job.then) parts.push(`→ ${childLabel(job.then, jobs)}`);
	return parts.join(" · ");
}

function childLabel(id: string, jobs: readonly JobEntry[] | undefined): string {
	const child = jobs?.find((row) => row.id === id);
	const instruction = child?.instruction.trim() ?? "";
	return instruction !== "" ? instruction : id;
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
	return `${outcomeWord(job.last_status)} ${when}${deliverySuffix(job.last_delivery, job.last_delivery_error)}`;
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

/** True when the last slot was skipped for lateness. Not a failure. */
export function jobMissed(job: JobEntry): boolean {
	return job.last_status === "missed";
}

/** True when the last fire is parked on an approval card. Not a failure. */
export function jobWaiting(job: JobEntry): boolean {
	return job.last_status === "waiting";
}

/** True when the last regular slot was blocked by a local calendar. */
export function jobSkipped(job: JobEntry): boolean {
	return job.last_status === "skipped";
}

function outcomeWord(status: JobEntry["last_status"] | JobRunEntry["status"]): string {
	if (status === "failed") return "Failed";
	if (status === "missed") return "Missed";
	if (status === "waiting") return "Waiting for approval";
	if (status === "skipped") return "Skipped (calendar)";
	return "Ran";
}

/**
 * One history row: local time, Ran, Failed, Missed, Waiting for
 * approval, or Skipped (calendar), and "manual" only when the fire was
 * Run now. A scheduled fire is the default, so naming it adds nothing.
 * Missed is a late slot. Waiting is an approval card. Skipped is a day
 * blocked on a local calendar. None of those is a failed turn.
 */
export function jobRunLabel(run: JobRunEntry, timeZone?: string): string {
	const when = formatLocal(run.started_at, timeZone);
	const outcome = outcomeWord(run.status);
	const attempt = attemptSuffix(run);
	let label: string;
	if (run.trigger === "manual") label = `${outcome} ${when} · manual${attempt}`;
	else if (run.trigger === "chained") {
		// Not the slot, and not Run now. The note names the job that started this one.
		const note = run.note?.trim() ? ` · ${run.note.trim()}` : "";
		label = `${outcome} ${when} · chained${note}${attempt}`;
	} else label = `${outcome} ${when}${attempt}`;
	return label + deliverySuffix(run.delivery, run.delivery_error);
}

/** Delivery is separate from the run. Only a failed send is worth a suffix. */
function deliverySuffix(
	delivery: "ok" | "failed" | null | undefined,
	error: string | null | undefined,
): string {
	if (delivery !== "failed") return "";
	const detail = (error ?? "").trim();
	return detail !== "" ? ` · delivery failed (${detail})` : " · delivery failed";
}

/** History names the try only when the job retries. Run now has no number. */
function attemptSuffix(run: JobRunEntry): string {
	if (run.attempt == null || run.attempts == null) return "";
	return ` · attempt ${run.attempt} of ${run.attempts}`;
}

/** The rail no longer lists the session a run recorded. */
export function sessionMissingCopy(): string {
	return "Session no longer exists";
}

function blankToNull(value: string): string | undefined {
	const text = value.trim();
	return text === "" ? undefined : text;
}

/** Wire payload for a new job. Empty cadence / next_run / pin are omitted. */
export function saveFromDraft(
	draft: JobDraftFields,
	timezone: string | undefined = viewerTimeZone(),
): SaveJob {
	const payload: SaveJob = {
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
	const preset = blankToNull(draft.preset);
	if (preset !== undefined) payload.preset = preset;
	if (draft.engine === "native" || draft.engine === "grok") payload.engine = draft.engine;
	// Blank is "always run", the same as omitting the field on create.
	const grace = draft.grace.trim();
	if (grace !== "") payload.grace = grace;
	const retries = retryCount(draft.retries);
	if (retries !== undefined) {
		payload.retries = retries;
		payload.retry_delay = RETRY_DELAY;
	}
	// Blank is no follow-on, the same as omitting the field on create.
	const follow = draft.then.trim();
	if (follow !== "") payload.then = follow;
	// Blank is no calendar, the same as omitting the field on create.
	const calendar = draft.skip_calendar.trim();
	if (calendar !== "") payload.skip_calendar = calendar;
	const match = draft.skip_match.trim();
	if (match !== "") payload.skip_match = match;
	// Blank is the configured limit, the same as omitting the field on create.
	const maxRun = draft.max_run.trim();
	if (maxRun !== "") payload.max_run = maxRun;
	// Only an email job names an address. A window create must not grow a field.
	if (draft.deliver_to === "email") payload.email_to = draft.email_to.trim();
	return payload;
}

function retryCount(raw: string): number | undefined {
	const text = raw.trim();
	if (text === "") return undefined;
	const count = Number(text);
	if (!Number.isInteger(count) || count < 1) return undefined;
	return count;
}

/**
 * Wire payload to replace a listed job, including pause.
 *
 * Preset and engine are omitted on purpose. Pause must not resend them:
 * a catalog name that has since been removed would fail the save, and
 * the job could not be paused. The calendar path is omitted for the
 * same reason: Pause must not clear it or demand the file still exist.
 */
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

/** Form copy. Create stays the new-job wording; edit names the save. */
export function jobFormCopy(editing: boolean): { title: string; lede: string; submit: string } {
	if (editing) {
		return {
			title: "Edit job",
			lede: "Change the fields, then save. Cadence or next run, not both.",
			submit: "Save",
		};
	}
	return {
		title: "New job",
		lede: "Parse a sentence, edit the draft, then create. Cadence or next run, not both.",
		submit: "Create",
	};
}

/**
 * Load a row into the form.
 *
 * A cadence job's `next_run` is the slot the runner armed, not a time the
 * user typed. Leaving it blank is what keeps Save from sending it back.
 */
export function draftFromJob(job: JobEntry): JobDraftFields {
	const recurring = typeof job.cadence === "string" && job.cadence.trim() !== "";
	return {
		workspace: job.workspace,
		instruction: job.instruction,
		cadence: recurring ? job.cadence ?? "" : "",
		next_run: recurring ? "" : (job.next_run ?? ""),
		deliver_to: job.deliver_to,
		email_to: job.email_to ?? "",
		paused: job.paused,
		preset: job.preset ?? "",
		engine: job.engine ?? "",
		grace: graceDraftValue(job.grace),
		retries: retriesDraftValue(job.retries),
		then: job.then ?? "",
		skip_calendar: job.skip_calendar ?? "",
		skip_match: job.skip_match ?? "",
		max_run: maxRunDraftValue(job.max_run),
	};
}

/**
 * Wire payload for an in-place edit.
 *
 * A blank cadence is sent as `""` because omitting it means "keep", and
 * that is the only way a recurring job becomes a one-shot. `next_run` is
 * sent only for a one-shot: on a cadence job the field is the armed slot
 * and the daemon keeps or re-arms it. The viewer zone goes out only for a
 * legacy job (no zone stored) whose cadence text changed — any other edit
 * keeps the zone the job already has.
 *
 * `paused` is the row's, not the draft's. The form has no pause control,
 * and Pause on the row can flip while the form is open.
 */
export function saveFromEdit(
	draft: JobDraftFields,
	job: JobEntry,
	timezone: string | undefined = viewerTimeZone(),
): SaveJob {
	const cadence = draft.cadence.trim();
	const nextRun = draft.next_run.trim();
	const payload: SaveJob = {
		type: "save_job",
		id: job.id,
		workspace: blankToNull(draft.workspace),
		instruction: blankToNull(draft.instruction),
		deliver_to: draft.deliver_to,
		paused: job.paused,
	};
	if (cadence === "") {
		payload.cadence = "";
		payload.next_run = nextRun;
	} else {
		payload.cadence = cadence;
	}
	const legacy = job.timezone == null;
	const cadenceChanged = cadence !== (job.cadence ?? "").trim();
	if (legacy && cadenceChanged && timezone !== undefined && timezone !== "") {
		payload.timezone = timezone;
	}
	// Always sent. `""` clears a pin back to "use current"; omitting it
	// would keep the stored one, which is Pause, not Save.
	payload.preset = draft.preset.trim();
	payload.engine = draft.engine;
	// Always sent. `""` clears a grace back to always run; omitting it
	// would keep the stored one, which is Pause, not Save.
	payload.grace = draft.grace.trim();
	// Always sent. `0` and a blank delay clear retries. A count sends the
	// 10-minute gap the select means; omitting both would keep the stored
	// policy, which is Pause, not Save.
	const retries = retryCount(draft.retries);
	if (retries === undefined) {
		payload.retries = 0;
		payload.retry_delay = "";
	} else {
		payload.retries = retries;
		payload.retry_delay = RETRY_DELAY;
	}
	// Always sent. `""` clears a follow-on; omitting it would keep the
	// stored one, which is Pause, not Save.
	payload.then = draft.then.trim();
	// Always sent. `""` clears a calendar; omitting it would keep the
	// stored path, which is Pause, not Save.
	payload.skip_calendar = draft.skip_calendar.trim();
	payload.skip_match = draft.skip_match.trim();
	// Always sent. `""` clears a limit back to the config; omitting it
	// would keep the stored one, which is Pause, not Save.
	payload.max_run = draft.max_run.trim();
	// Always sent. `""` clears an address; omitting it would keep the stored
	// one, which is Pause, not Save. A non-email channel clears it too.
	payload.email_to = draft.deliver_to === "email" ? draft.email_to.trim() : "";
	return payload;
}
