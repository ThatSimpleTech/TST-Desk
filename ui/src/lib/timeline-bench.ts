// Timeline render benchmark (TD-1404: `timeline_render_1000`).
//
// Measures the timeline at a thousand entries: push 1000 synthesized daemon
// events through the REAL store (timeline-store.svelte.ts → timeline.ts),
// then mount the REAL ActivityTimeline component and flush one frame. The
// push loop is the cost that scales with entry count — shell_output chunks
// linear-scan for their parent row — while the mount is windowed by design
// (virtualization keeps the DOM small; ~30 rows render for a 640px viewport).
//
// Runs under vitest's jsdom environment in the gate (timeline-bench.test.ts
// asserts against core/tests/perf_baselines.json). jsdom measures Svelte's
// DOM work — not browser layout or paint — so the baseline metadata says so.

import { mount, tick, unmount } from "svelte";
import type { DaemonEventUnion } from "./protocol";
import * as store from "./timeline-store.svelte.js";

/** ResizeObserver stub: jsdom has none, and ActivityTimeline bind:clientHeight
 * needs one. The stub's observe() also lies about the height (640px) so the
 * virtualization window renders a real row slice instead of zero rows. */
class StubResizeObserver {
  private cb: ResizeObserverCallback;
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb;
  }
  observe(el: Element): void {
    Object.defineProperty(el, "clientHeight", { get: () => 640, configurable: true });
    this.cb([], this as unknown as ResizeObserver);
  }
  unobserve(): void {}
  disconnect(): void {}
}

/** The session the synthesized log belongs to. */
const BENCH_SESSION = "bench";

export function installJsdomEnv(): void {
  if (typeof globalThis.ResizeObserver === "undefined") {
    globalThis.ResizeObserver = StubResizeObserver as unknown as typeof ResizeObserver;
  }
}

/** A realistic 1000-event session: user/assistant turns with tool calls, their
 * shell_output chunks (the linear-scan path), results, and the occasional
 * decision/error. Seq is monotonic like the daemon's. */
export function synthesizeEvents(n: number): DaemonEventUnion[] {
  const events: DaemonEventUnion[] = [];
  let seq = 0;
  let callId = 0;
  while (events.length < n) {
    const next = (type: string, extra: Record<string, unknown>): DaemonEventUnion =>
      ({ type, session_id: BENCH_SESSION, seq: ++seq, ...extra }) as DaemonEventUnion;
    events.push(next("assistant_delta", { delta: `assistant text ${seq} ` }));
    if (events.length >= n) break;
    if (events.length % 4 === 0) {
      const id = `tc-${++callId}`;
      events.push(
        next("tool_call", {
          tool_call_id: id,
          name: "shell",
          arguments: { command: `echo line ${callId}` },
          decision_class: "C",
        }),
      );
      for (let c = 0; c < 2 && events.length < n; c++) {
        events.push(
          next("shell_output", { tool_call_id: id, stream: "stdout", chunk: `out ${c}\n` }),
        );
      }
      events.push(
        next("tool_result", { tool_call_id: id, status: "success", output: "ok", truncated: false }),
      );
    }
    if (events.length % 25 === 0 && events.length < n) {
      events.push(
        next("decision_logged", {
          decision_class: "A",
          what: `benchmark decision ${seq}`,
          why: "synthetic load",
          commit: null,
        }),
      );
    }
    if (events.length % 50 === 0 && events.length < n) {
      events.push(next("error", { code: "bench_error", message: "synthetic", session_id: BENCH_SESSION }));
    }
  }
  return events;
}

/** One repetition: seconds for the 1000-event push plus mount + one flush. */
async function benchOnce(events: DaemonEventUnion[]): Promise<number> {
  // The store only folds the bound session's events (TD-1009), and the
  // synthesized log is session "bench".
  store.bindSession(BENCH_SESSION);
  const t0 = performance.now();
  for (const e of events) store.push(e);
  const target = document.body.appendChild(document.createElement("div"));
  const app = mount((await import("./components/ActivityTimeline.svelte")).default, { target });
  await tick();
  unmount(app);
  target.remove();
  const elapsed = performance.now() - t0;
  store.clear();
  return elapsed / 1000;
}

/** Median of `reps` repetitions over `entries` events (seconds). */
export async function benchTimelineRender(entries = 1000, reps = 5): Promise<number> {
  installJsdomEnv();
  const events = synthesizeEvents(entries);
  const samples: number[] = [];
  for (let i = 0; i < reps; i++) samples.push(await benchOnce(events));
  samples.sort((a, b) => a - b);
  return samples[Math.floor(samples.length / 2)];
}
