// Notification store (TD-1008).
//
// Owns two reactive lists: transient toasts (auto-expire after
// TOAST_TIMEOUT_MS) and persistent banners (dismissible; only explicit
// dismissal or a state change clears them). The connection sink feeds every
// validated daemon event through `notifyEvent`, which maps failures to
// tailored copy via error-copy.ts — toasts for transient trouble, banners
// for anything blocking until the user acts.
//
// Dedupe is by `key`: a repeated cause (e.g. the same cap pause after each
// denied resume) updates the existing notice instead of stacking. Banner
// dismissal only clears the UI; the next occurrence re-raises it.
//
// `copyDiagnostics` builds the redacted, pasteable report (AC): connection
// and session state, cost totals, the live notification list, and a short
// recent-event summary — never message content, tool arguments, or
// filesystem paths. Free-text fields that originate daemon-side (session
// reason, notification bodies) go through redact() here as the belt to
// TD-1405's suspenders: the UI never trusts that wire text arrived clean.

import pkg from "../../package.json";
import {
	checkpointNoticeCopy,
	daemonErrorCopy,
	sessionStateCopy,
	turnFailureCopy,
	type NoticeSpec,
} from "./error-copy";
import type { DaemonEventUnion } from "./protocol";
import { redact } from "./redact";
import { session } from "./session-status.svelte.js";

/** Milliseconds a toast lives before auto-dismissal. Exported for tests. */
export const TOAST_TIMEOUT_MS = 6000;

export interface Notice extends NoticeSpec {
	/** Monotonic id for component keys and dismissal. */
	id: number;
	/** Dedupe key: the same cause replaces an existing notice. */
	key: string;
}

export const toasts: Notice[] = $state([]);
export const banners: Notice[] = $state([]);

let nextId = 1;
const toastTimers = new Map<number, ReturnType<typeof setTimeout>>();

/** Recent events, kept for the diagnostics report. Types + seqs only. */
const MAX_RECENT_EVENTS = 25;
const recentEvents: { type: string; seq: number | null }[] = [];

/** Versions from the daemon's `ready` event, for the diagnostics report. */
let daemonVersion: string | null = null;
let protocolVersion: number | null = null;

/**
 * Raise a notice. An existing notice with the same key is replaced (and a
 * toast's timer restarts), so a repeating failure never stacks duplicates.
 */
export function notify(key: string, spec: NoticeSpec): Notice {
	const list = spec.severity === "banner" ? banners : toasts;
	const existing = list.findIndex((n) => n.key === key);
	if (existing >= 0) {
		const prior = list[existing];
		list[existing] = { ...spec, id: prior.id, key };
		if (spec.severity === "toast") startToastTimer(prior.id);
		return list[existing];
	}
	const notice: Notice = { ...spec, id: nextId++, key };
	list.push(notice);
	if (spec.severity === "toast") startToastTimer(notice.id);
	return notice;
}

/** Remove a notice by id from whichever list holds it. */
export function dismiss(id: number): void {
	const timer = toastTimers.get(id);
	if (timer !== undefined) {
		clearTimeout(timer);
		toastTimers.delete(id);
	}
	for (const list of [toasts, banners]) {
		const i = list.findIndex((n) => n.id === id);
		if (i >= 0) {
			list.splice(i, 1);
			return;
		}
	}
}

/** Drop every notice (connection teardown, tests). */
export function clearNotifications(): void {
	for (const timer of toastTimers.values()) clearTimeout(timer);
	toastTimers.clear();
	toasts.length = 0;
	banners.length = 0;
}

function startToastTimer(id: number): void {
	const prior = toastTimers.get(id);
	if (prior !== undefined) clearTimeout(prior);
	toastTimers.set(
		id,
		setTimeout(() => {
			toastTimers.delete(id);
			dismiss(id);
		}, TOAST_TIMEOUT_MS),
	);
}

/**
 * Reduce one daemon event into notices. Purely event-driven — invalid or
 * irrelevant events are ignored, and a user-initiated result (a cancelled
 * turn) never raises anything.
 */
export function notifyEvent(event: DaemonEventUnion): void {
	recentEvents.push({ type: event.type, seq: "seq" in event ? event.seq : null });
	if (recentEvents.length > MAX_RECENT_EVENTS) recentEvents.shift();

	switch (event.type) {
		case "ready":
			daemonVersion = event.version;
			protocolVersion = event.protocol_version;
			break;
		case "turn_complete":
			if (!event.failed) break;
			{
				const spec = turnFailureCopy(event.error_code);
				if (spec !== null) notify(`turn:${event.error_code}`, spec);
			}
			break;
		case "session_state": {
			const spec = sessionStateCopy(event.state, event.reason ?? null);
			if (event.state === "paused" && spec !== null) {
				notify(`paused:${spec.title}`, spec);
			} else if (event.state === "failed") {
				notify("failed", spec ?? { severity: "banner", title: "Session failed", body: "The session ended unexpectedly." });
			} else if (event.state === "interrupted" && spec !== null) {
				notify("interrupted", spec);
			} else if (event.state === "running") {
				// Resume clears the blockers — reaching running again is the fix.
				for (const n of [...banners]) {
					if (n.key.startsWith("paused:") || n.key.startsWith("turn:")) dismiss(n.id);
				}
			}
			break;
		}
		// TD-705 / TD-2104: checkpointing or memory versioning degraded for
		// this workspace. The daemon sends each code once per session; before
		// this case the event passed the client gate and died at the sink, so
		// a user with a non-git workspace was never told undo was off.
		case "checkpoint_notice":
			notify(`checkpoint:${event.code}`, checkpointNoticeCopy(event.code, event.message));
			break;
		case "error":
			notify(`daemon:${event.code}`, daemonErrorCopy(event.code, event.message));
			break;
	}
}

// ── Diagnostics ──────────────────────────────────────────────────────────

/**
 * Connection slice of the diagnostics report, passed in by the caller
 * (components read it live from connection-status). Keeps this module
 * one-directional: stores get fed, they never reach back into the
 * connection layer.
 */
export interface ConnectionSnapshot {
	ws: string;
	daemonState: string;
	daemonRestart: number;
}

/**
 * Redacted diagnostics snapshot: states, versions, cost totals, live
 * notifications, recent event types+seqs. Deliberately excludes workspace
 * paths, message content, and tool arguments; the wire-derived text it does
 * include (session reason, notification bodies) passes through redact() so
 * the report stays safe to paste anywhere (TD-1008).
 */
export function buildDiagnostics(conn: ConnectionSnapshot): string {
	const lines = [
		"## tst-desk diagnostics",
		`app: ui ${pkg.version}, daemon ${daemonVersion ?? "unknown"}, protocol ${protocolVersion ?? "unknown"}`,
		`connection: ws ${conn.ws}, daemon ${conn.daemonState}${conn.daemonRestart > 0 ? ` (restarted ${conn.daemonRestart}x)` : ""}`,
		`session: ${session.state}${session.reason ? ` (${redact(session.reason)})` : ""}, tier ${session.tier}${session.tierOverride ? ` [pinned: ${session.tierOverride}]` : ""}`,
		`cost: turn $${session.cost.turn.toFixed(4)}, session $${session.cost.session.toFixed(4)}, total $${session.cost.total.toFixed(4)}`,
	];
	for (const n of [...banners, ...toasts]) {
		lines.push(`notification [${n.severity}] ${n.title}: ${redact(n.body)}`);
	}
	if (recentEvents.length > 0) {
		lines.push(`recent events (${recentEvents.length}): ${recentEvents.map((e) => (e.seq !== null ? `${e.type}#${e.seq}` : e.type)).join(", ")}`);
	}
	return lines.join("\n");
}

/**
 * Write the diagnostics report to the clipboard. Returns false when the
 * clipboard is unavailable so the caller can fall back to showing the text.
 */
export async function copyDiagnostics(
	conn: ConnectionSnapshot,
	clip?: { writeText(text: string): Promise<void> },
): Promise<boolean> {
	const target = clip ?? (typeof navigator !== "undefined" ? navigator.clipboard : undefined);
	if (target === undefined) return false;
	try {
		await target.writeText(buildDiagnostics(conn));
		return true;
	} catch {
		return false;
	}
}
