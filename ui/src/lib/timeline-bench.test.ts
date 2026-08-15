// @vitest-environment jsdom

// Performance regression gate for the timeline (TD-1404).
//
// Mirrors core/tests/test_benchmarks.py: a fresh median fails when it exceeds
// 3x the committed baseline or baseline + 250 ms, whichever is larger, with
// the baseline living in the same shared core/tests/perf_baselines.json.
// Core pytest cannot mount a Svelte component, so this is the only place the
// `timeline_render_1000` metric can be honestly measured — and gated.
//
// Baselines are re-recorded deliberately, never by CI:
//   BENCH_RECORD=1 npx vitest run src/lib/timeline-bench.test.ts
// records a fresh median into perf_baselines.json instead of asserting.

import { describe, it, expect } from "vitest";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { benchTimelineRender } from "./timeline-bench";

const BASELINE_PATH = resolve(process.cwd(), "../core/tests/perf_baselines.json");
const METRIC = "timeline_render_1000";

// Same rule as the Python gate — keep in sync with test_benchmarks.py.
const REGRESSION_FACTOR = 3.0;
const REGRESSION_SLACK_S = 0.25;

interface Baselines {
  recorded_at: string;
  note: string;
  metrics: Record<string, number | null>;
  pending: Record<string, string>;
  ui_measured?: string[];
}

function loadBaselines(): Baselines {
  return JSON.parse(readFileSync(BASELINE_PATH, "utf-8")) as Baselines;
}

describe("timeline render at 1000 entries (TD-1404)", () => {
  it("stays within 3x / +250ms of the committed baseline", async () => {
    const measured = await benchTimelineRender(1000, 5);

    if (process.env.BENCH_RECORD === "1") {
      // Deliberate re-baselining: write the fresh median, clear the pending
      // marker, declare the metric UI-measured — never touch the core rows.
      const payload = loadBaselines();
      payload.metrics[METRIC] = measured;
      delete payload.pending[METRIC];
      payload.ui_measured = [METRIC];
      payload.note =
        "first_token_latency uses an instant mock provider: it measures the internal " +
        "pipeline, not network or model time. timeline_render_1000 is measured by the UI " +
        "bench under jsdom: Svelte DOM work, not browser layout or paint.";
      writeFileSync(BASELINE_PATH, JSON.stringify(payload, null, 2) + "\n");
      expect(measured).toBeGreaterThan(0);
      return;
    }

    const baseline = loadBaselines().metrics[METRIC];
    expect(
      baseline,
      `${METRIC} has no baseline — record one with BENCH_RECORD=1`,
    ).not.toBeNull();

    const ceiling = Math.max(
      baseline! * REGRESSION_FACTOR,
      baseline! + REGRESSION_SLACK_S,
    );
    expect(
      measured,
      `${METRIC} regressed: ${measured.toFixed(3)}s vs baseline ${baseline!.toFixed(3)}s ` +
        `(ceiling ${ceiling.toFixed(3)}s = 3x baseline or +${REGRESSION_SLACK_S}s)`,
    ).toBeLessThanOrEqual(ceiling);
  }, 30_000);
});
