// Background shell jobs per session (TD-5137), from the daemon's
// `shell_jobs` events. The daemon owns the jobs; the window only lists
// them, so the newest event is the list and nothing here is inferred.

import { onEvent } from "./connection-status.svelte.js";
import type { DaemonEventUnion, ShellJobInfo } from "./protocol";

export const shellJobs = $state({ bySession: {} as Record<string, ShellJobInfo[]> });

const NONE: readonly ShellJobInfo[] = [];

export function shellJobsFor(sessionId: string | null): readonly ShellJobInfo[] {
	if (sessionId === null) return NONE;
	return shellJobs.bySession[sessionId] ?? NONE;
}

/** "running", "exited 0", "stopped", "lost": what the list shows per job. */
export function jobStateLabel(job: ShellJobInfo): string {
	if (job.status === "exited") return `exited ${job.exit_code ?? "?"}`;
	return job.status;
}

let started = false;

export function startShellJobs(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

export function resetShellJobs(): void {
	started = false;
	shellJobs.bySession = {};
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "shell_jobs") return;
	shellJobs.bySession[event.session_id] = event.jobs ?? [];
}
