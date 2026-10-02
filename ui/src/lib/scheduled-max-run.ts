// Max run time on the job form (TD-3819).
//
// The select sends a phrase the daemon already parses for grace. Blank
// is the configured limit. A stored count the select does not list still
// round-trips, or Save would clear it.

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** Phrase for a stored second count. Grace uses the same spelling. */
export function durationPhrase(seconds: number): string {
	if (seconds > 0 && seconds % DAY === 0) {
		const count = seconds / DAY;
		return `${count} ${count === 1 ? "day" : "days"}`;
	}
	if (seconds > 0 && seconds % HOUR === 0) {
		const count = seconds / HOUR;
		return `${count} ${count === 1 ? "hour" : "hours"}`;
	}
	if (seconds > 0 && seconds % MINUTE === 0) {
		const count = seconds / MINUTE;
		return `${count} ${count === 1 ? "minute" : "minutes"}`;
	}
	return String(seconds);
}

/** Default uses `scheduler.max_run_seconds`. The others are this job's limit. */
export const MAX_RUN_CHOICES: readonly { value: string; label: string; seconds: number | null }[] = [
	{ value: "", label: "Default", seconds: null },
	{ value: "5 minutes", label: "5 min", seconds: 5 * 60 },
	{ value: "10 minutes", label: "10 min", seconds: 10 * 60 },
	{ value: "15 minutes", label: "15 min", seconds: 15 * 60 },
	{ value: "30 minutes", label: "30 min", seconds: 30 * 60 },
	{ value: "60 minutes", label: "60 min", seconds: 60 * 60 },
];

/** Draft value for a stored limit. Unknown counts still round-trip on Save. */
export function maxRunDraftValue(seconds: number | null | undefined): string {
	if (seconds == null) return "";
	const known = MAX_RUN_CHOICES.find((choice) => choice.seconds === seconds);
	if (known !== undefined) return known.value;
	return durationPhrase(seconds);
}
