// Tests for OS notifications (TD-1702).
//
// The store talks to the OS only through an injected bridge, so these
// cases do not need Tauri. They pin the four acceptance criteria:
// unfocused approval, unfocused turn complete, silence when focused,
// and a single lazy permission ask.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, JobEntry } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (_msg: ClientMessageUnion) => true,
}));

import { noticeActionMessage, noticeFor, runStamps, scheduledNotices, shouldNotify } from "./os-notify";
import {
	handleOsEvent,
	resetOsNotify,
	setWindowFocused,
	startOsNotify,
	type OsNotifyBridge,
} from "./os-notify.svelte.js";

function approval(): DaemonEventUnion {
	return {
		type: "approval_request",
		session_id: "s1",
		seq: 2,
		tool_call_id: "tc-1",
		tool_name: "shell",
		arguments: { command: "rm -rf build/" },
		decision_class: "B",
		summary: "Run `rm -rf build/`",
		reason: "decision class B requires approval",
	} as DaemonEventUnion;
}

function turn(failed = false): DaemonEventUnion {
	return {
		type: "turn_complete",
		session_id: "s1",
		seq: 3,
		tokens: 10,
		cost: 0,
		tier: "brain",
		duration: 1,
		failed,
		error_code: failed ? "auth_failed" : null,
	} as DaemonEventUnion;
}

function bridge() {
	const sent: { title: string; body: string }[] = [];
	const impl: OsNotifyBridge = {
		isPermissionGranted: vi.fn(async () => false),
		requestPermission: vi.fn(async () => "granted"),
		send: vi.fn(async (n) => {
			sent.push(n);
		}),
	};
	return { impl, sent };
}

beforeEach(() => {
	resetOsNotify();
	mocks.handler = null;
});

describe("noticeFor", () => {
	it("describes an approval card", () => {
		const n = noticeFor(approval());
		expect(n?.title).toBe("Approval needed");
		expect(n?.body).toBe("Run `rm -rf build/`");
		expect(n?.kind).toBe("approval");
		expect(n?.sessionId).toBe("s1");
		expect(n?.toolCallId).toBe("tc-1");
		expect(n?.actions).toEqual([
			{ id: "approve", title: "Approve" },
			{ id: "deny", title: "Deny" },
		]);
	});

	it("does not put Approve on a class-C card — those open the window", () => {
		const n = noticeFor({ ...approval(), decision_class: "C" } as DaemonEventUnion);
		expect(n?.actions).toBeUndefined();
	});

	it("maps Approve/Deny onto the same verbs the card sends", () => {
		expect(noticeActionMessage("approve", "s1", "tc-1")).toEqual({
			type: "approve",
			session_id: "s1",
			tool_call_id: "tc-1",
		});
		expect(noticeActionMessage("deny", "s1", "tc-1").type).toBe("deny");
	});

	it("describes a finished turn", () => {
		expect(noticeFor(turn())?.title).toBe("Turn complete");
		expect(noticeFor(turn(true))?.title).toBe("Turn failed");
	});

	it("ignores everything else", () => {
		expect(noticeFor({ type: "assistant_delta", session_id: "s1", seq: 1, delta: "x" } as DaemonEventUnion)).toBeNull();
	});

	it("redacts key-shaped strings before they reach a banner (TD-4801)", () => {
		const key = "sk-or-v1-" + "0".repeat(52); // tst-secret-ok
		const n = noticeFor({ ...approval(), summary: `Run deploy with ${key}` } as DaemonEventUnion);
		expect(n?.body).toContain("[REDACTED]");
		expect(n?.body).not.toContain(key);
	});
});

describe("shouldNotify", () => {
	it("is silent when the window is focused", () => {
		expect(shouldNotify(true)).toBe(false);
	});

	it("fires when the window is hidden (unfocused)", () => {
		expect(shouldNotify(false)).toBe(true);
	});
});

describe("handleOsEvent", () => {
	it("does not notify or ask while focused", async () => {
		const { impl, sent } = bridge();
		startOsNotify(impl);
		setWindowFocused(true);
		await handleOsEvent(approval());
		expect(sent).toEqual([]);
		expect(impl.requestPermission).not.toHaveBeenCalled();
	});

	it("notifies an unfocused approval after a lazy permission ask", async () => {
		const { impl, sent } = bridge();
		startOsNotify(impl);
		setWindowFocused(false);
		await handleOsEvent(approval());
		expect(impl.isPermissionGranted).toHaveBeenCalledOnce();
		expect(impl.requestPermission).toHaveBeenCalledOnce();
		expect(sent[0]?.title).toBe("Approval needed");
		expect(sent[0]?.body).toBe("Run `rm -rf build/`");
		expect(sent[0]).toMatchObject({
			sessionId: "s1",
			toolCallId: "tc-1",
		});
	});

	it("notifies an unfocused turn completion", async () => {
		const { impl, sent } = bridge();
		impl.isPermissionGranted = vi.fn(async () => true);
		startOsNotify(impl);
		setWindowFocused(false);
		await handleOsEvent(turn());
		expect(impl.requestPermission).not.toHaveBeenCalled();
		expect(sent[0]?.title).toBe("Turn complete");
	});

	it("asks permission only on the first qualifying event", async () => {
		const { impl } = bridge();
		startOsNotify(impl);
		setWindowFocused(false);
		await handleOsEvent(approval());
		await handleOsEvent(turn());
		expect(impl.requestPermission).toHaveBeenCalledOnce();
		expect(impl.send).toHaveBeenCalledTimes(2);
	});

	it("sends nothing when permission is denied", async () => {
		const { impl, sent } = bridge();
		impl.requestPermission = vi.fn(async () => "denied");
		startOsNotify(impl);
		setWindowFocused(false);
		await handleOsEvent(approval());
		expect(sent).toEqual([]);
	});
});

// A scheduled job fires when nobody is watching, so the run receipt on the
// pane is not delivery for `deliver_to: "window"` (TD-3807).
function jobRow(over: Partial<JobEntry> & Pick<JobEntry, "id">): JobEntry {
	return {
		workspace: "/ws/proj",
		instruction: "summarize the inbox",
		cadence: "every 1 hour",
		next_run: null,
		deliver_to: "window",
		paused: false,
		last_run: null,
		last_status: null,
		last_summary: null,
		last_session_id: null,
		...over,
	};
}

function jobList(jobs: JobEntry[], seq = 1): DaemonEventUnion {
	return { type: "job_list", seq, jobs } as DaemonEventUnion;
}

const RAN_ONCE = jobRow({
	id: "j1",
	last_run: "2026-08-21T18:00:00+00:00",
	last_status: "ok",
	last_summary: "3 new messages, none urgent",
});

describe("scheduledNotices", () => {
	it("says nothing about a job it has not seen before", () => {
		// Reconnecting must not ring for every run in the job's history.
		expect(scheduledNotices(new Map(), [RAN_ONCE])).toEqual([]);
	});

	it("announces a run that landed since the last list", () => {
		const before = runStamps([jobRow({ id: "j1" })]);
		expect(scheduledNotices(before, [RAN_ONCE])).toEqual([
			{
				title: "Scheduled job ran",
				body: "3 new messages, none urgent",
				kind: "scheduled",
			},
		]);
	});

	it("says nothing when the same run is listed again", () => {
		const before = runStamps([RAN_ONCE]);
		expect(scheduledNotices(before, [RAN_ONCE])).toEqual([]);
	});

	it("marks a failure as one", () => {
		const before = runStamps([jobRow({ id: "j1" })]);
		const failed = { ...RAN_ONCE, last_status: "failed", last_summary: "workspace is gone" };
		expect(scheduledNotices(before, [failed as JobEntry])[0]).toMatchObject({
			title: "Scheduled job failed",
			body: "workspace is gone",
		});
	});

	it("leaves slack and ntfy alone — they already delivered themselves", () => {
		const before = runStamps([jobRow({ id: "j1", deliver_to: "slack" })]);
		const ran = { ...RAN_ONCE, deliver_to: "slack" } as JobEntry;
		expect(scheduledNotices(before, [ran])).toEqual([]);
	});

	it("falls back to the instruction when the run said nothing", () => {
		const before = runStamps([jobRow({ id: "j1" })]);
		const quiet = { ...RAN_ONCE, last_summary: null } as JobEntry;
		expect(scheduledNotices(before, [quiet])[0]?.body).toBe("summarize the inbox");
	});

	it("clamps a long summary to a glance and redacts it", () => {
		const key = "sk-or-v1-" + "0".repeat(52); // tst-secret-ok
		const before = runStamps([jobRow({ id: "j1" })]);
		const long = { ...RAN_ONCE, last_summary: `${key} ${"x".repeat(4000)}` } as JobEntry;
		const body = scheduledNotices(before, [long])[0]?.body ?? "";
		expect(body).not.toContain(key);
		expect(body).toContain("[REDACTED]");
		expect(body.length).toBeLessThanOrEqual(200);
		expect(body.endsWith("…")).toBe(true);
	});
});

describe("handleOsEvent for scheduled runs", () => {
	it("seeds silently, then announces the next run", async () => {
		const { impl, sent } = bridge();
		impl.isPermissionGranted = vi.fn(async () => true);
		startOsNotify(impl);
		setWindowFocused(false);

		await handleOsEvent(jobList([jobRow({ id: "j1" })]));
		expect(sent).toEqual([]);

		await handleOsEvent(jobList([RAN_ONCE], 2));
		expect(sent).toEqual([
			{ title: "Scheduled job ran", body: "3 new messages, none urgent" },
		]);
	});

	it("tracks runs the user watched, so the next list is not a fresh run", async () => {
		const { impl, sent } = bridge();
		impl.isPermissionGranted = vi.fn(async () => true);
		startOsNotify(impl);

		// Focused: the user saw this land on the pane.
		setWindowFocused(true);
		await handleOsEvent(jobList([jobRow({ id: "j1" })]));
		await handleOsEvent(jobList([RAN_ONCE], 2));

		// Backgrounded, and nothing new has run since.
		setWindowFocused(false);
		await handleOsEvent(jobList([RAN_ONCE], 3));
		expect(sent).toEqual([]);
	});

	it("announces every job that fired in one tick", async () => {
		const { impl, sent } = bridge();
		impl.isPermissionGranted = vi.fn(async () => true);
		startOsNotify(impl);
		setWindowFocused(false);

		await handleOsEvent(jobList([jobRow({ id: "j1" }), jobRow({ id: "j2" })]));
		await handleOsEvent(
			jobList([RAN_ONCE, { ...RAN_ONCE, id: "j2", last_summary: "build is green" }], 2),
		);
		expect(sent.map((n) => n.body)).toEqual(["3 new messages, none urgent", "build is green"]);
	});
});
