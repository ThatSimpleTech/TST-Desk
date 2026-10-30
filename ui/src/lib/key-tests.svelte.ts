// Settings → API keys: each saved key's Test result, on its own row (TD-5200).
//
// The daemon answers one connection's messages in order, so each verdict
// answers the oldest validate_api_key this window still waits on. `asked`
// lists them, oldest first, which ties every verdict to its own request: a
// row tested again ignores its earlier test's late answer, and an older
// daemon's verdict, which names no key, still finds the row that asked. A
// daemon from TD-5200 on echoes the credential, which picks out the entry.
// The first-run check asks through here too, so neither takes the other's.
//
// The 30-second clock runs only for the oldest request, the one the daemon
// is working on, so a test queued behind another is not blamed for the wait.
// A result lasts until setup_state changes what its test was about, the
// window reaches another computer, or the row is tested again. Fed by the
// settings reducer, so it rides the same event subscription.

import { onConnectionState, sendToDaemon } from "./connection-status.svelte.js";
import { hub } from "./hub.svelte.js";
import type {
	ApiKeyValidated,
	CredentialSummary,
	DaemonEventUnion,
	SetupState,
	ValidateApiKey,
} from "./protocol";

/** How long the request the daemon is working on may take to be answered.
 *  The daemon gives up on a probe sooner (KEY_TEST_LIMIT_S), so this only
 *  fires when the daemon is busy with something else or is an older one. */
export const KEY_TEST_TIMEOUT_MS = 30_000;

export const KEY_TEST_COPY = {
	rejected: "The key was rejected.",
	noAnswer:
		`Nothing came back within ${KEY_TEST_TIMEOUT_MS / 1000} seconds. ` +
		"A later answer will still show here.",
	dropped: "The connection to TST Desk dropped before the answer came. Test again.",
	notConnected: "TST Desk is not connected. Try again in a moment.",
} as const;

export type KeyTestResult = { state: "ok" | "failed" | "no_answer"; detail: string };
export type KeyTest = { state: "testing" } | KeyTestResult;
/** Who sent a validate_api_key: a row's credential id, or null for the first-run check. */
type Asker = string | null;

/** One entry per credential id that was tested in this window. */
export const keyTests = $state<Record<string, KeyTest>>({});

/** The validate_api_key requests still unanswered on this connection, oldest first. */
const asked: Asker[] = [];
let clock: ReturnType<typeof setTimeout> | null = null;
/** Each key's row as the last setup_state described it. */
let rows = new Map<string, string>();
/** The row each settled result is about; a later change to it drops the result. */
const settledOn = new Map<string, string>();
/** The computer the results came from (hub.machineId); undefined before any test. */
let testedOn: string | null | undefined;
let offConnection: (() => void) | null = null;
/** Who each verdict answered, worked out once per frame: the onboarding
 *  store asks too, and the two reducers may run in either order. */
const answered = new WeakMap<ApiKeyValidated, Asker | undefined>();

// The daemon's DEFAULT_CREDENTIAL_ID: an unbound remote tier sends it.
const DEFAULT_KEY = "openrouter";

/** The active preset's tiers that send key *id*, as resolve_credential_id picks. */
function tiersSending(state: SetupState, id: string): string[] {
	const bound = state.tier_credentials ?? {};
	const loopback = state.tier_loopback ?? {};
	const tiers = new Set([...Object.keys(bound), ...Object.keys(loopback)]);
	return [...tiers]
		.filter((tier) => {
			const key = bound[tier];
			return key ? key === id : id === DEFAULT_KEY && !loopback[tier];
		})
		.sort();
}

/** What a test of this key is about: the daemon picks its target from the
 *  key's host, the tiers that send it and the active preset (key_probe). */
function rowShape(row: CredentialSummary, state: SetupState): string {
	return JSON.stringify([
		row.name,
		row.base_url ?? null,
		row.stored,
		state.active_preset,
		tiersSending(state, row.id),
	]);
}

function forget(credential: string): void {
	settledOn.delete(credential);
	delete keyTests[credential];
}

/** Show *result* on the row as it stands now. A row that is gone shows nothing. */
function settle(credential: string, result: KeyTestResult): void {
	const shape = rows.get(credential);
	if (shape === undefined) {
		forget(credential);
		return;
	}
	settledOn.set(credential, shape);
	keyTests[credential] = result;
}

/** Time the request the daemon is answering now; with none, stop the clock. */
function restartClock(): void {
	if (clock !== null) clearTimeout(clock);
	clock = null;
	const head = asked[0];
	if (head === undefined) return;
	clock = setTimeout(() => {
		clock = null;
		if (head !== null && keyTests[head]?.state === "testing") {
			settle(head, { state: "no_answer", detail: KEY_TEST_COPY.noAnswer });
		}
	}, KEY_TEST_TIMEOUT_MS);
}

/** Take the entry at *index* off `asked`; the next one's clock starts. */
function takeAsked(index: number): Asker {
	const [asker] = asked.splice(index, 1);
	if (index === 0) restartClock();
	return asker ?? null;
}

/** A reply for row *credential*. Only the reply to its latest test shows:
 *  an earlier one answers a question the row has since asked again. A row
 *  with no entry changed since its test, so the reply is about another. */
function answer(credential: string, result: KeyTestResult): void {
	if (asked.includes(credential) || !(credential in keyTests)) return;
	settle(credential, result);
}

/** The connection dropped: nothing this window asked will be answered. */
function dropAsked(): void {
	const waiting = new Set(asked.filter((asker): asker is string => asker !== null));
	asked.length = 0;
	restartClock();
	for (const credential of waiting) {
		const state = keyTests[credential]?.state;
		if (state === "testing" || state === "no_answer") {
			settle(credential, { state: "no_answer", detail: KEY_TEST_COPY.dropped });
		}
	}
}

function watchConnection(): void {
	if (offConnection !== null) return;
	testedOn = hub.machineId;
	offConnection = onConnectionState((state) => {
		if (state !== "connected") {
			dropAsked();
			return;
		}
		// A phone paired with another computer reconnects in place; a result
		// from the first one says nothing about the second one's keys.
		if (hub.machineId === testedOn) return;
		testedOn = hub.machineId;
		settledOn.clear();
		for (const credential of Object.keys(keyTests)) delete keyTests[credential];
	});
}

/** Send a validate_api_key and note who asked. False when it was not sent. */
export function sendKeyCheck(msg: ValidateApiKey): boolean {
	watchConnection();
	if (!sendToDaemon(msg)) return false;
	asked.push(msg.credential ?? null);
	if (asked.length === 1) restartClock();
	return true;
}

/** The row's Test button: probe the saved key once and wait for its verdict. */
export function testKey(credential: string): void {
	if (keyTests[credential]?.state === "testing") return;
	settledOn.delete(credential);
	keyTests[credential] = { state: "testing" };
	if (!sendKeyCheck({ type: "validate_api_key", credential })) {
		settle(credential, { state: "no_answer", detail: KEY_TEST_COPY.notConnected });
	}
}

/** Who a verdict answers: a row's credential id, null for the first-run
 *  check, undefined when this window was not waiting on it. Settles the
 *  row. Safe to call again with the same frame. */
export function takeVerdict(event: ApiKeyValidated): Asker | undefined {
	if (answered.has(event)) return answered.get(event);
	// A daemon from TD-5200 on names the key, or null for the first-run
	// check. An older one sends no field: the oldest request is its question.
	let index = asked.length > 0 ? 0 : -1;
	if ("credential" in event) index = asked.indexOf(event.credential ?? null);
	const asker = index === -1 ? undefined : takeAsked(index);
	answered.set(event, asker);
	if (typeof asker === "string") {
		answer(
			asker,
			event.ok
				? { state: "ok", detail: event.detail }
				: { state: "failed", detail: event.detail || KEY_TEST_COPY.rejected },
		);
	}
	return asker;
}

export function reduceKeyTests(event: DaemonEventUnion): void {
	if (event.type === "setup_state") {
		rows = new Map((event.credentials ?? []).map((row) => [row.id, rowShape(row, event)]));
		for (const credential of Object.keys(keyTests)) {
			// A test in flight is settled against the row its verdict finds: a
			// host committed on the way to the Test button is acked before it.
			if (keyTests[credential]?.state === "testing") continue;
			if (rows.get(credential) !== settledOn.get(credential)) forget(credential);
		}
		return;
	}
	if (event.type === "api_key_validated") {
		takeVerdict(event);
		return;
	}
	// A refused validate_api_key is answered in the same order a verdict is.
	if (event.type === "error" && event.in_reply_to === "validate_api_key" && !event.machine) {
		if (asked.length === 0) return;
		const asker = takeAsked(0);
		if (asker !== null) answer(asker, { state: "failed", detail: event.message });
	}
}

/** Reset for tests (the settings store's reset calls it). */
export function resetKeyTests(): void {
	asked.length = 0;
	restartClock();
	offConnection?.();
	offConnection = null;
	testedOn = undefined;
	settledOn.clear();
	rows = new Map();
	for (const credential of Object.keys(keyTests)) delete keyTests[credential];
}
