// Context gauge and `/compact` (TD-5109).
//
// Pure helpers so the gauge and the command are testable without mounting
// anything. Every figure comes from the daemon's `context_usage` event: the
// UI never estimates tokens itself.

import type { NoticeSpec } from "./error-copy";
import type { ContextUsage } from "./protocol";

/** What the gauge shows: the last `context_usage`, without the envelope. */
export type ContextReading = Pick<ContextUsage, "tier" | "used_tokens" | "context_window" | "compact_at">;

/** The daemon keeps this much guidance; more is cut there anyway. */
export const COMPACT_GUIDANCE_MAX = 500;

/**
 * `/compact` or `/compact <what to keep>`, alone in the draft. Returns the
 * guidance (possibly empty), or null when the draft is anything else.
 */
export function parseCompactCommand(text: string): { guidance: string } | null {
	const match = text.match(/^\s*\/compact(?:\s+([\s\S]*))?$/i);
	if (match === null) return null;
	return { guidance: (match[1] ?? "").trim().slice(0, COMPACT_GUIDANCE_MAX) };
}

/**
 * The toast when a compact request never left the window (TD-5154). Either a
 * turn is running, or no live connection took it. The daemon's own refusals
 * come back as error frames with their own copy.
 */
export function compactNotSentCopy(turnRunning: boolean): NoticeSpec {
	return {
		severity: "toast",
		title: "Not compacted",
		body: turnRunning
			? "A turn is running. Compact when it finishes, or stop it first."
			: "TST Desk isn't connected to its background service. Compact again once it reconnects.",
	};
}

/**
 * Short token count: "980", "1.2k", "12k", "1.5M". Windows are usually
 * powers of two, so an exact multiple of 1024 is divided by 1024 (65,536
 * reads "64k", the size people know it by); anything else by 1000.
 */
export function formatTokens(n: number): string {
	const value = Math.max(0, Math.round(n));
	if (value < 1000) return String(value);
	if (value >= 1_000_000) return `${trimZero((value / 1_000_000).toFixed(1))}M`;
	const k = value % 1024 === 0 ? value / 1024 : value / 1000;
	return k < 10 ? `${trimZero(k.toFixed(1))}k` : `${Math.round(k)}k`;
}

function trimZero(s: string): string {
	return s.replace(/\.0$/, "");
}

/** "12k / 64k" for the title bar. */
export function gaugeLabel(reading: ContextReading): string {
	return `${formatTokens(reading.used_tokens)} / ${formatTokens(reading.context_window)}`;
}

/** Share of the whole window in use, 0-100. */
export function gaugePercent(reading: ContextReading): number {
	if (reading.context_window <= 0) return 0;
	return Math.min(100, Math.round((reading.used_tokens / reading.context_window) * 100));
}

/**
 * "ok" well short of auto-compaction, "near" in its last quarter, "over"
 * once the next request would compact.
 */
export function gaugeLevel(reading: ContextReading): "ok" | "near" | "over" {
	if (reading.compact_at <= 0) return "ok";
	const ratio = reading.used_tokens / reading.compact_at;
	if (ratio >= 1) return "over";
	return ratio >= 0.75 ? "near" : "ok";
}
