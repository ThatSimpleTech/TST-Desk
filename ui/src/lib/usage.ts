// Usage view derivations (TD-1706).
//
// Pure and rune-free, like cost-format.ts, so the bucketing and the money
// are unit-testable in node. Everything here reshapes rows the daemon
// already sent — nothing computes a cost the UI was not given (AGENTS §6:
// the UI never derives truth it wasn't given).

import type { UsageRollup } from "./protocol";

export type UsageBucket = "session" | "day" | "week";

export const USAGE_BUCKETS: readonly UsageBucket[] = ["session", "day", "week"];

export const BUCKET_LABELS: Record<UsageBucket, string> = {
	session: "Session",
	day: "Day",
	week: "Week",
};

/** One bucket with its per-tier split and the totals across those tiers. */
export interface UsageGroup {
	key: string;
	rows: UsageRollup[];
	tokens: number;
	cost: number;
	classifierCost: number;
}

/** Every token a row moved — cached reads included, since the model
 *  processed them and the audit store counted them. */
export function rowTokens(row: UsageRollup): number {
	return row.prompt_tokens + row.cached_prompt_tokens + row.completion_tokens;
}

/** Rows for one bucket kind, grouped by key, order preserved.
 *
 *  The daemon sends every bucket in one message and already ordered them
 *  most-recent-first, so this filters and folds rather than re-sorting —
 *  re-sorting by key here would put session ids in alphabetical order and
 *  silently claim that was chronological.
 */
export function groupByBucket(rows: UsageRollup[], bucket: UsageBucket): UsageGroup[] {
	const groups: UsageGroup[] = [];
	const byKey = new Map<string, UsageGroup>();
	for (const row of rows) {
		if (row.bucket !== bucket) continue;
		let group = byKey.get(row.key);
		if (group === undefined) {
			group = { key: row.key, rows: [], tokens: 0, cost: 0, classifierCost: 0 };
			byKey.set(row.key, group);
			groups.push(group);
		}
		group.rows.push(row);
		group.tokens += rowTokens(row);
		group.cost += row.cost;
		group.classifierCost += row.classifier_cost;
	}
	return groups;
}

/** Totals across every group — the "all time in view" line. */
export function totalsFor(groups: UsageGroup[]): {
	tokens: number;
	cost: number;
	classifierCost: number;
} {
	return groups.reduce(
		(acc, g) => ({
			tokens: acc.tokens + g.tokens,
			cost: acc.cost + g.cost,
			classifierCost: acc.classifierCost + g.classifierCost,
		}),
		{ tokens: 0, cost: 0, classifierCost: 0 },
	);
}

/** Token counts, abbreviated. Costs keep their own formatter (formatUsd):
 *  tokens tolerate rounding, money does not. */
export function formatTokens(n: number): string {
	if (n < 1000) return `${n}`;
	if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
	return `${(n / 1_000_000).toFixed(2)}M`;
}

/** A bucket key as the panel shows it. Session ids are long and only the
 *  tail distinguishes them; days and weeks are already readable, and a
 *  week is labelled by the Monday it opened on. */
export function formatBucketKey(bucket: UsageBucket, key: string): string {
	if (bucket === "session") return key.length > 12 ? `…${key.slice(-8)}` : key;
	if (bucket === "week") return `week of ${key}`;
	return key;
}
