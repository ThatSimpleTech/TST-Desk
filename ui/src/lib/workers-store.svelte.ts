// Background delegate workers per session (TD-5299), from the daemon's
// `worker_jobs` and `worker_result` events. The daemon owns them; the
// window lists them, shows a settled worker's steps, and sends Stop.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, WorkerJobInfo, WorkerResult } from "./protocol";

export const workers = $state({
	bySession: {} as Record<string, WorkerJobInfo[]>,
	results: {} as Record<string, WorkerResult>,
});

const NONE: readonly WorkerJobInfo[] = [];
const LIVE = new Set(["queued", "running", "awaiting_approval"]);

export function workersFor(sessionId: string | null): readonly WorkerJobInfo[] {
	if (sessionId === null) return NONE;
	return workers.bySession[sessionId] ?? NONE;
}

export function isLive(worker: WorkerJobInfo): boolean {
	return LIVE.has(worker.state);
}

/** "running", "waiting for you", "done", "stopped (why)": one line per worker. */
export function workerStateLabel(worker: WorkerJobInfo): string {
	if (worker.state === "awaiting_approval") return "waiting for you";
	if (worker.state === "stopped" && worker.stopped) return `stopped (${worker.stopped})`;
	return worker.state;
}

export function resultFor(sessionId: string, workerId: string): WorkerResult | undefined {
	return workers.results[`${sessionId}/${workerId}`];
}

/** Stop one worker. False when nothing was sent. */
export function stopWorker(sessionId: string, workerId: string): boolean {
	return sendToDaemon({ type: "worker_stop", session_id: sessionId, worker_id: workerId });
}

let started = false;

export function startWorkers(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetWorkers(): void {
	started = false;
	workers.bySession = {};
	workers.results = {};
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "worker_jobs") {
		workers.bySession[event.session_id] = event.workers ?? [];
		return;
	}
	if (event.type === "worker_result") {
		workers.results[`${event.session_id}/${event.worker_id}`] = event;
	}
}
