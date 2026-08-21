// Tests for OS notifications (TD-1702).
//
// The store talks to the OS only through an injected bridge, so these
// cases do not need Tauri. They pin the four acceptance criteria:
// unfocused approval, unfocused turn complete, silence when focused,
// and a single lazy permission ask.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

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

import { noticeFor, shouldNotify } from "./os-notify";
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
		expect(n).toEqual({
			title: "Approval needed",
			body: "Run `rm -rf build/`",
			kind: "approval",
		});
	});

	it("describes a finished turn", () => {
		expect(noticeFor(turn())?.title).toBe("Turn complete");
		expect(noticeFor(turn(true))?.title).toBe("Turn failed");
	});

	it("ignores everything else", () => {
		expect(noticeFor({ type: "assistant_delta", session_id: "s1", seq: 1, delta: "x" } as DaemonEventUnion)).toBeNull();
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
		expect(sent).toEqual([{ title: "Approval needed", body: "Run `rm -rf build/`" }]);
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
