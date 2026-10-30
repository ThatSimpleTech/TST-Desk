// Settings → API keys: each saved key's Test result, on its own row (TD-5200).
//
// Results are kept per credential id. The daemon echoes the id on
// api_key_validated, so a verdict lands on the row that asked and never on
// another. A verdict that names no key is the first-run check's, and the
// onboarding store keeps it. A result lasts until setup_state reports a new
// name, host or stored state for that key, or the row is tested again.
// Fed by the settings reducer, so it rides the same event subscription.

import { sendToDaemon } from "./connection-status.svelte.js";
import type { CredentialSummary, DaemonEventUnion } from "./protocol";

/** How long a row waits for its verdict before saying none came. */
export const KEY_TEST_TIMEOUT_MS = 30_000;

export const KEY_TEST_COPY = {
	rejected: "The key was rejected.",
	noAnswer: `TST Desk did not answer within ${KEY_TEST_TIMEOUT_MS / 1000} seconds. Try again.`,
	notConnected: "TST Desk is not connected. Try again in a moment.",
} as const;

export type KeyTestResult = { state: "ok" | "failed" | "no_answer"; detail: string };
export type KeyTest = { state: "testing" } | KeyTestResult;

/** One entry per credential id that was tested in this window. */
export const keyTests = $state<Record<string, KeyTest>>({});

/** Each key's row as the last setup_state described it. */
let rows = new Map<string, string>();
/** The row each settled result is about; a later change to it drops the result. */
const settledOn = new Map<string, string>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function rowShape(row: CredentialSummary): string {
	return JSON.stringify([row.name, row.base_url ?? null, row.stored]);
}

function clearTimer(credential: string): void {
	const timer = timers.get(credential);
	if (timer !== undefined) clearTimeout(timer);
	timers.delete(credential);
}

function forget(credential: string): void {
	clearTimer(credential);
	settledOn.delete(credential);
	delete keyTests[credential];
}

/** Show *result* on the row as it stands now. A row that is gone shows nothing. */
function settle(credential: string, result: KeyTestResult): void {
	clearTimer(credential);
	const shape = rows.get(credential);
	if (shape === undefined) {
		forget(credential);
		return;
	}
	settledOn.set(credential, shape);
	keyTests[credential] = result;
}

/** The row's Test button: probe the saved key once and wait for its verdict. */
export function testKey(credential: string): void {
	if (keyTests[credential]?.state === "testing") return;
	clearTimer(credential);
	settledOn.delete(credential);
	keyTests[credential] = { state: "testing" };
	if (!sendToDaemon({ type: "validate_api_key", credential })) {
		settle(credential, { state: "no_answer", detail: KEY_TEST_COPY.notConnected });
		return;
	}
	timers.set(
		credential,
		setTimeout(() => {
			timers.delete(credential);
			if (keyTests[credential]?.state !== "testing") return;
			settle(credential, { state: "no_answer", detail: KEY_TEST_COPY.noAnswer });
		}, KEY_TEST_TIMEOUT_MS),
	);
}

export function reduceKeyTests(event: DaemonEventUnion): void {
	if (event.type === "setup_state") {
		rows = new Map((event.credentials ?? []).map((row) => [row.id, rowShape(row)]));
		for (const credential of Object.keys(keyTests)) {
			// A test in flight is settled against the row its verdict finds: a
			// host committed on the way to the Test button is acked before it.
			if (keyTests[credential]?.state === "testing") continue;
			if (rows.get(credential) !== settledOn.get(credential)) forget(credential);
		}
		return;
	}
	if (event.type !== "api_key_validated") return;
	const credential = event.credential ?? null;
	// No entry: the first-run check's verdict (it names no key), or one about
	// a name, host or key the row has changed since. Neither belongs on a row.
	// A row that gave up waiting still takes its late verdict.
	if (credential === null || !(credential in keyTests)) return;
	settle(
		credential,
		event.ok
			? { state: "ok", detail: event.detail }
			: { state: "failed", detail: event.detail || KEY_TEST_COPY.rejected },
	);
}

/** Reset for tests (the settings store's reset calls it). */
export function resetKeyTests(): void {
	for (const timer of timers.values()) clearTimeout(timer);
	timers.clear();
	settledOn.clear();
	rows = new Map();
	for (const credential of Object.keys(keyTests)) delete keyTests[credential];
}
