// Design mode store (TD-3403).
//
// Freezes the last `screen_frame` preview. Clicks become picks; a CU tool
// in flight forces the mode off so the pane is a watch surface again.
// Hit-test is injectable: tests (and CI) supply a scripted node; live
// sends `design_hit_test` and enriches the chip when `design_hit` arrives.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import {
	applyPicks,
	canRunDesign,
	geometricHitNode,
	scriptedHitNode,
	type CssBox,
	type DesignPick,
	type DesignSurface,
	type HitNode,
	type PickMode,
} from "./design";
import { isActuatingCuTool, isBrowserTool, isCuTool } from "./screen";
import { screen } from "./screen.svelte.js";
import { session } from "./session-status.svelte.js";
import type { DaemonEventUnion, DesignHit, ToolCall } from "./protocol";

export const design = $state({
	enabled: false,
	actuating: false,
	surface: null as DesignSurface,
	frozenPreview: null as string | null,
	picks: [] as DesignPick[],
});

export type DesignHitTester = (x: number, y: number) => HitNode | Promise<HitNode>;

let started = false;
let stopEvents: (() => void) | null = null;
let nextPickId = 0;
let hitTester: DesignHitTester | null = null;
const toolNames = new Map<string, string>();
const inFlightCu = new Set<string>();

function ensureStarted(): void {
	if (started) return;
	started = true;
	stopEvents = onEvent(reduce);
}

export function startDesign(): () => void {
	ensureStarted();
	return () => {
		started = false;
		stopEvents?.();
		stopEvents = null;
	};
}

export function resetDesign(): void {
	started = false;
	stopEvents?.();
	stopEvents = null;
	nextPickId = 0;
	hitTester = null;
	toolNames.clear();
	inFlightCu.clear();
	design.enabled = false;
	design.actuating = false;
	design.surface = null;
	design.frozenPreview = null;
	design.picks = [];
}

export function setDesignHitTester(tester: DesignHitTester | null): void {
	hitTester = tester;
}

export function toggleDesign(): boolean {
	ensureStarted();
	if (design.enabled) {
		design.enabled = false;
		return false;
	}
	if (!canRunDesign({ actuating: design.actuating, hasPreview: screen.preview !== null })) {
		return false;
	}
	design.frozenPreview = screen.preview;
	design.enabled = true;
	return true;
}

export async function addPick(args: {
	x: number;
	y: number;
	box?: CssBox;
	cropDataUrl: string;
	mode: PickMode;
}): Promise<DesignPick | null> {
	ensureStarted();
	if (!design.enabled || design.actuating) return null;
	const node = await resolveHit(args.x, args.y, args.box);
	const box = args.box ?? node.box;
	nextPickId += 1;
	const pick: DesignPick = {
		id: `pick-${nextPickId}`,
		xpath: node.xpath,
		role: node.role,
		attributes: node.attributes,
		box,
		styles: node.styles,
		cropDataUrl: args.cropDataUrl,
	};
	design.picks = applyPicks(design.picks, pick, args.mode);
	if (hitTester === null && design.surface === "browser" && session.sessionId !== null) {
		sendToDaemon({
			type: "design_hit_test",
			session_id: session.sessionId,
			x: args.x,
			y: args.y,
		});
	}
	return pick;
}

export function removePick(id: string): void {
	design.picks = design.picks.filter((p) => p.id !== id);
}

export function clearPicks(): void {
	design.picks = [];
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "tool_call") {
		rememberTool(event);
		return;
	}
	if (event.type === "tool_result") {
		if (inFlightCu.delete(event.tool_call_id)) {
			design.actuating = inFlightCu.size > 0;
			if (design.actuating && design.enabled) turnOff();
		}
		return;
	}
	if (event.type === "design_hit") {
		enrichPick(event);
		return;
	}
}

function rememberTool(event: ToolCall): void {
	toolNames.set(event.tool_call_id, event.name);
	if (!isCuTool(event.name)) return;
	design.surface = isBrowserTool(event.name) ? "browser" : "desktop";
	if (!isActuatingCuTool(event.name)) return;
	inFlightCu.add(event.tool_call_id);
	design.actuating = true;
	if (design.enabled) turnOff();
}

function turnOff(): void {
	design.enabled = false;
}

function enrichPick(event: DesignHit): void {
	const match = [...design.picks]
		.reverse()
		.find((p) => near(p.box, event.x, event.y) || nearPoint(event.x, event.y, p));
	if (match === undefined) return;
	match.xpath = event.xpath ?? match.xpath;
	match.role = event.role ?? match.role;
	if (Object.keys(event.attributes).length > 0) match.attributes = event.attributes;
	if (Object.keys(event.styles).length > 0) match.styles = event.styles;
	if (event.box !== null && event.box !== undefined) {
		match.box = event.box;
	}
	design.picks = [...design.picks];
}

function near(box: CssBox, x: number, y: number): boolean {
	return x >= box.x && y >= box.y && x <= box.x + box.width && y <= box.y + box.height;
}

function nearPoint(x: number, y: number, pick: DesignPick): boolean {
	const cx = pick.box.x + pick.box.width / 2;
	const cy = pick.box.y + pick.box.height / 2;
	return Math.hypot(cx - x, cy - y) < 2;
}

async function resolveHit(x: number, y: number, box?: CssBox): Promise<HitNode> {
	if (hitTester !== null) return hitTester(x, y);
	if (design.surface === "desktop") {
		return geometricHitNode(box ?? { x, y, width: 1, height: 1 });
	}
	return scriptedHitNode(x, y);
}
