// Computer-use glow / agent cursor helpers (TD-3402).
//
// Pure: when the Screen pane should show indicators, how reduced-motion
// changes them, and where the software cursor sits on the frame.
// The overlays are CSS on the pane — not a second hardware pointer.

import { isCuTool } from "./screen";

export function reducedMotionIndicators(reduce: boolean): {
	staticGlow: boolean;
	trail: boolean;
} {
	return { staticGlow: reduce, trail: !reduce };
}

export function cuIndicatorsLive(args: {
	killed: boolean;
	inFlightCu: number;
	cuTurnActive: boolean;
}): boolean {
	if (args.killed) return false;
	return args.inFlightCu > 0 || args.cuTurnActive;
}

export function isInFlightCuTool(name: string): boolean {
	return isCuTool(name);
}

export function pointFromArgs(
	args: Record<string, unknown>,
): { x: number; y: number } | null {
	const x = args.x;
	const y = args.y;
	if (typeof x !== "number" || typeof y !== "number") return null;
	if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
	return { x, y };
}

export function cursorPercent(
	x: number | null,
	y: number | null,
	width: number | null,
	height: number | null,
): { left: number; top: number } {
	if (x === null || y === null || width === null || height === null) {
		return { left: 50, top: 50 };
	}
	if (width <= 0 || height <= 0) return { left: 50, top: 50 };
	return {
		left: clamp((x / width) * 100, 0, 100),
		top: clamp((y / height) * 100, 0, 100),
	};
}

function clamp(value: number, min: number, max: number): number {
	return Math.min(max, Math.max(min, value));
}
