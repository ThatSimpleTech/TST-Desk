// Doctor store (TD-1104 diagnostics).
//
// The AppShell header button opens the pane and fires `run_diagnostics`;
// the daemon answers with one `diagnostics_report` after all checks finish
// (the provider probe is a live one-token call, so this can take a second).
// The pane renders rows; "Copy report" puts the report on the clipboard as
// redacted plain text — the same redact() pass TD-1008's diagnostics use.
//
// Wiring mirrors TD-1101: listens on the connection fan-out, sends only
// via sendToDaemon — no client reference, no import cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { redact } from "./redact";
import type { DaemonEventUnion, DiagnosticCheck } from "./protocol";

export const doctor = $state({
	open: false,
	/** True between the request and the report arriving. */
	running: false,
	checks: [] as DiagnosticCheck[],
});

let started = false;

/** Register the reducer once. Returns the unsubscribe for tests. */
export function startDoctor(): () => void {
	if (started) return () => {};
	started = true;
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetDoctor(): void {
	doctor.open = false;
	doctor.running = false;
	doctor.checks = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type !== "diagnostics_report") return;
	doctor.running = false;
	doctor.checks = event.checks;
}

/** Open the pane and run the checks. A click while running is ignored. */
export function runDoctor(): void {
	doctor.open = true;
	if (doctor.running) return;
	doctor.running = true;
	const sent = sendToDaemon({ type: "run_diagnostics" });
	if (!sent) {
		doctor.running = false;
		doctor.checks = [
			{
				name: "daemon",
				status: "fail",
				detail: "no connection to the daemon",
				fix: "Restart the app; if it keeps happening, grab the daemon log.",
			},
		];
	}
}

export function closeDoctor(): void {
	doctor.open = false;
}

/** Glyph per status — shared by the pane rows and the copied report. */
export const STATUS_MARK: Record<DiagnosticCheck["status"], string> = {
	ok: "✓",
	fail: "✗",
	skip: "–",
};

/** The pasteable report: one line per check, fix lines indented under fails. */
export function buildDoctorReport(checks: DiagnosticCheck[]): string {
	const lines = ["TST Desk doctor", ""];
	for (const c of checks) {
		lines.push(`${STATUS_MARK[c.status]} ${c.name} — ${c.detail}`);
		if (c.fix) lines.push(`  fix: ${c.fix}`);
	}
	return redact(lines.join("\n"));
}

/** Copy the report; injectable clipboard for tests (mirrors copyDiagnostics). */
export async function copyDoctorReport(
	clip?: { writeText(text: string): Promise<void> },
): Promise<boolean> {
	const target = clip ?? (typeof navigator !== "undefined" ? navigator.clipboard : undefined);
	if (target === undefined) return false;
	try {
		await target.writeText(buildDoctorReport(doctor.checks));
		return true;
	} catch {
		return false;
	}
}
