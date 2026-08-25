// Screen-pane indicator store (TD-3402).
//
// Live while a desktop_* / browser_* tool is in flight, or the session
// is still in a CU turn after one. Clears on turn_complete, cancelled,
// and cu_kill_state killed=true. Prefs live in the settings store.

import { onEvent } from "./connection-status.svelte.js";
import type { DaemonEventUnion, ToolCall } from "./protocol";
import {
	cuIndicatorsLive,
	isInFlightCuTool,
	pointFromArgs,
} from "./screen-indicator";

export const cuIndicators = $state({
	sessionId: null as string | null,
	live: false,
	cursorX: null as number | null,
	cursorY: null as number | null,
	frameWidth: null as number | null,
	frameHeight: null as number | null,
	prefersReducedMotion: false,
});

let started = false;
let stopEvents: (() => void) | null = null;
let stopMotion: (() => void) | null = null;
const inFlight = new Set<string>();
const toolNames = new Map<string, string>();
let cuTurnActive = false;
let killed = false;

function recompute(): void {
	cuIndicators.live = cuIndicatorsLive({
		killed,
		inFlightCu: inFlight.size,
		cuTurnActive,
	});
}

function clearLive(): void {
	inFlight.clear();
	toolNames.clear();
	cuTurnActive = false;
	cuIndicators.live = false;
}

function sameSession(sessionId: string): boolean {
	return cuIndicators.sessionId === null || cuIndicators.sessionId === sessionId;
}

export function startCuIndicators(): () => void {
	if (started) return () => {};
	started = true;
	stopEvents = onEvent(reduce);
	stopMotion = watchReducedMotion();
	return () => {
		started = false;
		stopEvents?.();
		stopEvents = null;
		stopMotion?.();
		stopMotion = null;
	};
}

export function resetCuIndicators(): void {
	started = false;
	stopEvents?.();
	stopEvents = null;
	stopMotion?.();
	stopMotion = null;
	inFlight.clear();
	toolNames.clear();
	cuTurnActive = false;
	killed = false;
	cuIndicators.sessionId = null;
	cuIndicators.live = false;
	cuIndicators.cursorX = null;
	cuIndicators.cursorY = null;
	cuIndicators.frameWidth = null;
	cuIndicators.frameHeight = null;
	cuIndicators.prefersReducedMotion = false;
}

function watchReducedMotion(): () => void {
	if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
		return () => {};
	}
	const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
	const apply = (): void => {
		cuIndicators.prefersReducedMotion = mq.matches;
	};
	apply();
	mq.addEventListener("change", apply);
	return () => mq.removeEventListener("change", apply);
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "tool_call") {
		rememberTool(event);
		return;
	}
	if (event.type === "tool_result") {
		const name = toolNames.get(event.tool_call_id);
		inFlight.delete(event.tool_call_id);
		if (name !== undefined && isInFlightCuTool(name) && sameSession(event.session_id)) {
			recompute();
		}
		return;
	}
	if (event.type === "screen_frame") {
		if (typeof event.width === "number") cuIndicators.frameWidth = event.width;
		if (typeof event.height === "number") cuIndicators.frameHeight = event.height;
		if (sameSession(event.session_id)) cuIndicators.sessionId = event.session_id;
		return;
	}
	if (event.type === "cu_session") {
		if (!sameSession(event.session_id)) return;
		cuIndicators.sessionId = event.session_id;
		cuTurnActive = event.active;
		if (!event.active) {
			inFlight.clear();
			toolNames.clear();
		}
		recompute();
		return;
	}
	if (event.type === "turn_complete") {
		if (sameSession(event.session_id)) clearLive();
		return;
	}
	if (event.type === "session_state" && event.state === "cancelled") {
		if (sameSession(event.session_id)) clearLive();
		return;
	}
	if (event.type === "cu_kill_state") {
		killed = event.killed;
		if (event.killed) clearLive();
		else recompute();
	}
}

function rememberTool(event: ToolCall): void {
	toolNames.set(event.tool_call_id, event.name);
	if (!isInFlightCuTool(event.name)) return;
	cuIndicators.sessionId = event.session_id;
	inFlight.add(event.tool_call_id);
	cuTurnActive = true;
	killed = false;
	const point = pointFromArgs(event.arguments);
	if (point !== null) {
		cuIndicators.cursorX = point.x;
		cuIndicators.cursorY = point.y;
	}
	recompute();
}
