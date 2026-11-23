// TD-5299: the window lists background workers from the daemon's newest
// `worker_jobs`, keeps each settled worker's steps, and only sends Stop.

import { beforeEach, describe, expect, it, vi } from "vitest";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[] };
});

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => (connection.sent.push(msg), true),
}));

import fixtures from "./protocol-fixtures.json";
import type { WorkerJobs, WorkerResult } from "./protocol";
import {
  isLive,
  resetWorkers,
  resultFor,
  startWorkers,
  stopWorker,
  workerStateLabel,
  workersFor,
} from "./workers-store.svelte.js";

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
}

beforeEach(() => {
  connection.sent.length = 0;
  connection.handlers.clear();
  resetWorkers();
  startWorkers();
});

describe("workers store", () => {
  it("the newest list is the truth, per session", () => {
    const jobs = fixtures.worker_jobs as WorkerJobs;
    emit(jobs);
    const [worker] = workersFor("sess-1");
    expect(worker.worker_id).toBe("w-1");
    expect(isLive(worker)).toBe(true);
    expect(workersFor("other")).toEqual([]);
    emit({ ...jobs, workers: [{ ...worker, state: "done" }] });
    expect(isLive(workersFor("sess-1")[0])).toBe(false);
  });

  it("keeps a settled worker's steps for the window", () => {
    emit(fixtures.worker_result as WorkerResult);
    expect(resultFor("sess-1", "w-1")?.transcript.length).toBe(1);
  });

  it("says why a worker stopped and when it waits for you", () => {
    const base = (fixtures.worker_jobs as WorkerJobs).workers[0];
    expect(workerStateLabel({ ...base, state: "awaiting_approval" })).toBe("waiting for you");
    expect(workerStateLabel({ ...base, state: "stopped", stopped: "delegate_stop" })).toBe(
      "stopped (delegate_stop)",
    );
  });

  it("Stop only sends the person's request", () => {
    expect(stopWorker("sess-1", "w-1")).toBe(true);
    expect(connection.sent).toEqual([
      { type: "worker_stop", session_id: "sess-1", worker_id: "w-1" },
    ]);
  });
});
