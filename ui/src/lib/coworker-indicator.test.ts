// @vitest-environment jsdom
//
// Coworker indicator (TD-2904). The decision is pure — these cases do not
// need a dock, a taskbar, or a tray. They pin: badge only while hidden and
// working, approval wins, reveal focuses the parked card, and idle/shown
// clears the badge.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	pending: [] as Array<{ toolCallId: string }>,
	chat: { sessionId: null as string | null, turnState: null as string | null },
	sessions: { rows: [] as Array<{ sessionId: string; state: string }> },
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	onDaemonEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	onConnectionState: () => () => {},
	onResume: () => () => {},
	sendToDaemon: (_msg: ClientMessageUnion) => true,
	ws: { state: "disconnected" },
	attachToSession: () => {},
	detachFromSession: () => {},
}));

vi.mock("./approval-store.svelte.js", () => ({
	pending: mocks.pending,
	bindApprovals: () => {},
}));

vi.mock("./chat-store.svelte.js", () => ({
	chat: mocks.chat,
	selectSession: () => {},
}));

vi.mock("./sessions.svelte.js", () => ({
	sessions: mocks.sessions,
	selectRow: () => {},
}));

vi.mock("./session-status.svelte.js", () => ({
	focusSession: () => {},
}));

import {
	APPROVAL_CARD_ATTR,
	approvalCardSelector,
	approvalFocusOnReveal,
	BADGE_APPROVAL,
	BADGE_RUNNING,
	coworkerBadge,
	firstAwaitingSession,
	focusApprovalCard,
	trayTooltip,
} from "./coworker-indicator";
import {
	resetCoworkerIndicator,
	setWindowHidden,
	startCoworkerIndicator,
	type CoworkerIndicatorBridge,
} from "./coworker-indicator.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

function sessionState(sessionId: string, state: string): DaemonEventUnion {
	return {
		type: "session_state",
		session_id: sessionId,
		state: state as "running",
		seq: 1,
	} as DaemonEventUnion;
}

function sessionList(
	rows: { session_id: string; state: string }[],
): DaemonEventUnion {
	return {
		type: "session_list",
		seq: 1,
		sessions: rows.map((r) => ({
			session_id: r.session_id,
			workspace_path: "/ws",
			state: r.state as "running",
			created_at: "2026-01-01T00:00:00Z",
			updated_at: "2026-01-01T00:00:00Z",
			event_count: 1,
			archived: false,
		})),
	} as DaemonEventUnion;
}

function bridge() {
	const badges: (string | null)[] = [];
	const focused: string[] = [];
	const switched: string[] = [];
	const impl: CoworkerIndicatorBridge = {
		setBadge: vi.fn(async (label) => {
			badges.push(label);
		}),
		focusCard: vi.fn((id) => {
			focused.push(id);
			return true;
		}),
		switchSession: vi.fn((id) => {
			switched.push(id);
		}),
	};
	return { impl, badges, focused, switched };
}

let stop = (): void => {};

beforeEach(() => {
	stop();
	resetCoworkerIndicator();
	mocks.handler = null;
	mocks.pending.splice(0, mocks.pending.length);
	mocks.chat.sessionId = null;
	mocks.chat.turnState = null;
	mocks.sessions.rows = [];
});

afterEach(() => {
	stop();
	stop = () => {};
});

describe("coworkerBadge", () => {
	it("is silent when the window is shown, even if a session is live", () => {
		expect(coworkerBadge(false, ["running"])).toBeNull();
		expect(coworkerBadge(false, ["awaiting_approval"])).toBeNull();
	});

	it("says Running while hidden and a session is running", () => {
		expect(coworkerBadge(true, ["idle", "running"])).toBe(BADGE_RUNNING);
	});

	it("says Approval needed while hidden and a session is awaiting", () => {
		expect(coworkerBadge(true, ["awaiting_approval"])).toBe(BADGE_APPROVAL);
	});

	it("lets a parked approval win over a running sibling", () => {
		expect(coworkerBadge(true, ["running", "awaiting_approval"])).toBe(
			BADGE_APPROVAL,
		);
	});

	it("clears when hidden but idle", () => {
		expect(coworkerBadge(true, ["idle", "complete", "paused"])).toBeNull();
		expect(coworkerBadge(true, [])).toBeNull();
	});
});

describe("trayTooltip", () => {
	it("is the product name when idle", () => {
		expect(trayTooltip(["idle"])).toBe("TST Desk");
	});

	it("counts running and parked approvals while the window is up", () => {
		expect(trayTooltip(["running", "running", "awaiting_approval"])).toBe(
			"TST Desk — 2 running · 1 approval",
		);
	});
});

describe("approvalFocusOnReveal", () => {
	it("does nothing until the window is shown", () => {
		expect(
			approvalFocusOnReveal({
				revealed: false,
				pendingToolCallIds: ["tc-1"],
				awaitingSessionId: "s2",
				boundSessionId: "s1",
			}),
		).toEqual({ sessionId: null, toolCallId: null });
	});

	it("focuses the parked card already on the page", () => {
		expect(
			approvalFocusOnReveal({
				revealed: true,
				pendingToolCallIds: ["tc-1", "tc-2"],
				awaitingSessionId: "s1",
				boundSessionId: "s1",
			}),
		).toEqual({ sessionId: null, toolCallId: "tc-1" });
	});

	it("switches to the awaiting session when its card is not mounted", () => {
		expect(
			approvalFocusOnReveal({
				revealed: true,
				pendingToolCallIds: [],
				awaitingSessionId: "s2",
				boundSessionId: "s1",
			}),
		).toEqual({ sessionId: "s2", toolCallId: null });
	});

	it("does not steal the bound session when nothing is parked", () => {
		expect(
			approvalFocusOnReveal({
				revealed: true,
				pendingToolCallIds: [],
				awaitingSessionId: "s1",
				boundSessionId: "s1",
			}),
		).toEqual({ sessionId: null, toolCallId: null });
	});
});

describe("firstAwaitingSession", () => {
	it("returns the first awaiting row", () => {
		expect(
			firstAwaitingSession([
				{ sessionId: "s1", state: "running" },
				{ sessionId: "s2", state: "awaiting_approval" },
			]),
		).toBe("s2");
		expect(firstAwaitingSession([{ sessionId: "s1", state: "running" }])).toBeNull();
	});
});

describe("focusApprovalCard", () => {
	it("focuses the parked card without a real dock", () => {
		const root = document.createElement("div");
		const card = document.createElement("article");
		card.setAttribute(APPROVAL_CARD_ATTR, "tc-1");
		card.tabIndex = -1;
		root.append(card);
		document.body.append(root);
		expect(approvalCardSelector("tc-1")).toBe(`[${APPROVAL_CARD_ATTR}="tc-1"]`);
		expect(focusApprovalCard("tc-1", root)).toBe(true);
		expect(document.activeElement).toBe(card);
		root.remove();
	});

	it("returns false when the card is not on the page", () => {
		expect(focusApprovalCard("missing", document.body)).toBe(false);
	});
});

describe("startCoworkerIndicator", () => {
	it("badges the host only while hidden and working", async () => {
		const { impl, badges } = bridge();
		stop = startCoworkerIndicator(impl);
		setWindowHidden(true);
		emit(sessionState("s1", "running"));
		await Promise.resolve();
		expect(badges).toEqual([null, BADGE_RUNNING]);
		emit(sessionState("s1", "awaiting_approval"));
		await Promise.resolve();
		expect(badges.at(-1)).toBe(BADGE_APPROVAL);
		setWindowHidden(false);
		await Promise.resolve();
		expect(badges.at(-1)).toBeNull();
	});

	it("clears when the hidden session goes idle", async () => {
		const { impl, badges } = bridge();
		stop = startCoworkerIndicator(impl);
		setWindowHidden(true);
		emit(sessionList([{ session_id: "s1", state: "running" }]));
		await Promise.resolve();
		expect(badges.at(-1)).toBe(BADGE_RUNNING);
		emit(sessionList([{ session_id: "s1", state: "complete" }]));
		await Promise.resolve();
		expect(badges.at(-1)).toBeNull();
	});

	it("focuses the parked card when the hidden window is shown", async () => {
		const { impl, focused, switched } = bridge();
		mocks.chat.sessionId = "s1";
		mocks.pending.push({ toolCallId: "tc-1" });
		stop = startCoworkerIndicator(impl);
		setWindowHidden(true);
		emit(sessionState("s1", "awaiting_approval"));
		setWindowHidden(false);
		expect(focused).toEqual(["tc-1"]);
		expect(switched).toEqual([]);
	});

	it("does not ask the host for a tray", () => {
		const { impl } = bridge();
		stop = startCoworkerIndicator(impl);
		expect(Object.keys(impl)).toEqual(["setBadge", "focusCard", "switchSession"]);
	});
});
