// Coworker indicator (TD-2904) and tray running-count (TD-4703).
//
// Pure: given window visibility and session states, either the dock should
// say the coworker is still working or it should not. The host invoke is
// the side effect. Tray tooltip (TD-4703) is always on and engine-agnostic.

export const BADGE_RUNNING = "Running";
export const BADGE_APPROVAL = "Approval needed";

export type CoworkerBadge = typeof BADGE_RUNNING | typeof BADGE_APPROVAL | null;

/** Sessions actively working or parked on approval — tray badge input. */
export function trayRunningCount(sessionStates: readonly string[]): number {
  return sessionStates.filter(
    (state) => state === "running" || state === "awaiting_approval",
  ).length;
}

/** Dock / taskbar copy while hidden, or null when idle or the window is up. */
export function coworkerBadge(
	windowHidden: boolean,
	sessionStates: readonly string[],
): CoworkerBadge {
	if (!windowHidden) return null;
	if (sessionStates.includes("awaiting_approval")) return BADGE_APPROVAL;
	if (sessionStates.includes("running")) return BADGE_RUNNING;
	return null;
}

export interface ApprovalFocus {
	/** Attach this session so its parked card can mount. */
	sessionId: string | null;
	/** Focus this already-mounted card. */
	toolCallId: string | null;
}

/** After the window is shown, which parked approval to land on. */
export function approvalFocusOnReveal(input: {
	revealed: boolean;
	pendingToolCallIds: readonly string[];
	awaitingSessionId: string | null;
	boundSessionId: string | null;
}): ApprovalFocus {
	if (!input.revealed) return { sessionId: null, toolCallId: null };
	const first = input.pendingToolCallIds[0];
	if (first !== undefined) {
		return { sessionId: null, toolCallId: first };
	}
	if (
		input.awaitingSessionId !== null &&
		input.awaitingSessionId !== input.boundSessionId
	) {
		return { sessionId: input.awaitingSessionId, toolCallId: null };
	}
	return { sessionId: null, toolCallId: null };
}

/** Menu-bar / tray tooltip. Independent of window visibility. */
export function trayTooltip(sessionStates: readonly string[]): string {
	const running = sessionStates.filter((state) => state === "running").length;
	const awaiting = sessionStates.filter((state) => state === "awaiting_approval").length;
	if (running === 0 && awaiting === 0) return "TST Desk";
	const parts: string[] = [];
	if (running > 0) parts.push(`${running} running`);
	if (awaiting > 0) {
		parts.push(`${awaiting} approval${awaiting === 1 ? "" : "s"}`);
	}
	return `TST Desk — ${parts.join(" · ")}`;
}

export function firstAwaitingSession(
	sessions: readonly { sessionId: string; state: string }[],
): string | null {
	return sessions.find((s) => s.state === "awaiting_approval")?.sessionId ?? null;
}

export const APPROVAL_CARD_ATTR = "data-approval-card";

export function approvalCardSelector(toolCallId: string): string {
	const escaped =
		typeof CSS !== "undefined" && typeof CSS.escape === "function"
			? CSS.escape(toolCallId)
			: toolCallId;
	return `[${APPROVAL_CARD_ATTR}="${escaped}"]`;
}

/** Focus the parked card. `root` is injectable so tests do not need a dock. */
export function focusApprovalCard(
	toolCallId: string,
	root: ParentNode | null = typeof document !== "undefined" ? document : null,
): boolean {
	if (root === null) return false;
	const el = root.querySelector(approvalCardSelector(toolCallId));
	if (!(el instanceof HTMLElement)) return false;
	el.focus();
	el.scrollIntoView?.({ block: "nearest" });
	return true;
}
