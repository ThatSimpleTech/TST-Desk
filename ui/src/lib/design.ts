// Design mode (TD-3403): freeze the last screen frame and point at it.
//
// Clicks map into the frame's CSS pixels. The chip is a text attachment
// (JSON + a data-URL crop) so it rides `user_message` under TD-1709 caps
// without a new wire type. Voice, React fiber, and source maps are out.

import { acceptAttachment, toBase64, type NewAttachment } from "./attachments";
import type { AttachmentLimits } from "./protocol";

export interface CssBox {
	x: number;
	y: number;
	width: number;
	height: number;
}

export interface CssPoint {
	x: number;
	y: number;
}

export interface HitNode {
	xpath: string | null;
	role: string | null;
	attributes: Record<string, string>;
	box: CssBox;
	styles: Record<string, string>;
}

export interface DesignPick {
	id: string;
	xpath: string | null;
	role: string | null;
	attributes: Record<string, string>;
	box: CssBox;
	styles: Record<string, string>;
	cropDataUrl: string;
}

export type DesignSurface = "browser" | "desktop" | null;

export type PickMode = "replace" | "add";

/** Movement (frame CSS pixels) that turns a shift-click into a shift-drag. */
export const DRAG_THRESHOLD_PX = 4;

/** Matches core/tstd/browser/protocol.py `scripted_hit_node`. */
export function scriptedHitNode(x: number, y: number): HitNode {
	const ix = Math.round(x);
	const iy = Math.round(y);
	return {
		xpath: `//*[@data-mock-point='${ix},${iy}']`,
		role: "button",
		attributes: { id: "mock-target", "data-x": String(ix), "data-y": String(iy) },
		box: { x: x - 20, y: y - 10, width: 80, height: 24 },
		styles: { display: "inline-block", "font-size": "14px" },
	};
}

/** Optimistic first paint before ``design_hit`` fills role / attributes. */
export function geometricHitNode(box: CssBox): HitNode {
	const x = Math.round(box.x);
	const y = Math.round(box.y);
	return {
		xpath: null,
		role: null,
		attributes: { "data-x": String(x), "data-y": String(y) },
		box,
		styles: {},
	};
}

export function canRunDesign(args: { actuating: boolean; hasPreview: boolean }): boolean {
	return !args.actuating && args.hasPreview;
}

export function containedDrawRect(
	elWidth: number,
	elHeight: number,
	naturalWidth: number,
	naturalHeight: number,
): { left: number; top: number; width: number; height: number; scale: number } {
	if (naturalWidth <= 0 || naturalHeight <= 0 || elWidth <= 0 || elHeight <= 0) {
		return { left: 0, top: 0, width: 0, height: 0, scale: 1 };
	}
	const scale = Math.min(elWidth / naturalWidth, elHeight / naturalHeight);
	const width = naturalWidth * scale;
	const height = naturalHeight * scale;
	return {
		left: (elWidth - width) / 2,
		top: (elHeight - height) / 2,
		width,
		height,
		scale,
	};
}

export function mapClientToFrame(
	clientX: number,
	clientY: number,
	imgRect: { left: number; top: number; width: number; height: number },
	naturalWidth: number,
	naturalHeight: number,
): CssPoint | null {
	const draw = containedDrawRect(imgRect.width, imgRect.height, naturalWidth, naturalHeight);
	if (draw.scale <= 0) return null;
	const x = (clientX - imgRect.left - draw.left) / draw.scale;
	const y = (clientY - imgRect.top - draw.top) / draw.scale;
	if (x < 0 || y < 0 || x > naturalWidth || y > naturalHeight) return null;
	return { x, y };
}

export function frameToOverlay(
	box: CssBox,
	imgRect: { width: number; height: number },
	naturalWidth: number,
	naturalHeight: number,
): CssBox {
	const draw = containedDrawRect(imgRect.width, imgRect.height, naturalWidth, naturalHeight);
	return {
		x: draw.left + box.x * draw.scale,
		y: draw.top + box.y * draw.scale,
		width: box.width * draw.scale,
		height: box.height * draw.scale,
	};
}

export function rectFromPoints(a: CssPoint, b: CssPoint): CssBox {
	const x = Math.min(a.x, b.x);
	const y = Math.min(a.y, b.y);
	return { x, y, width: Math.abs(b.x - a.x), height: Math.abs(b.y - a.y) };
}

export function clampBox(box: CssBox, frame: { width: number; height: number }): CssBox {
	const x = Math.min(Math.max(0, box.x), Math.max(0, frame.width));
	const y = Math.min(Math.max(0, box.y), Math.max(0, frame.height));
	const width = Math.min(Math.max(1, box.width), Math.max(1, frame.width - x));
	const height = Math.min(Math.max(1, box.height), Math.max(1, frame.height - y));
	return { x, y, width, height };
}

export function movedEnough(a: CssPoint, b: CssPoint): boolean {
	return Math.hypot(b.x - a.x, b.y - a.y) >= DRAG_THRESHOLD_PX;
}

export function pickLabel(pick: Pick<DesignPick, "xpath" | "role">): string {
	return pick.xpath ?? pick.role ?? "design pick";
}

export function pickFileName(index: number): string {
	return index === 0 ? "design-pick.json" : `design-pick-${index + 1}.json`;
}

export function pickPayload(pick: DesignPick): string {
	return `${JSON.stringify(
		{
			kind: "design_pick",
			xpath: pick.xpath,
			role: pick.role,
			attributes: pick.attributes,
			box: pick.box,
			styles: pick.styles,
			crop_data_url: pick.cropDataUrl,
		},
		null,
		2,
	)}\n`;
}

export function pickToDraft(pick: DesignPick, index: number): NewAttachment {
	const text = pickPayload(pick);
	const bytes = new TextEncoder().encode(text);
	return {
		name: pickFileName(index),
		size: bytes.length,
		content_b64: toBase64(bytes),
	};
}

export function picksToDrafts(picks: readonly DesignPick[]): NewAttachment[] {
	return picks.map((pick, i) => pickToDraft(pick, i));
}

/** Vet pick drafts against the same caps the composer uses. */
export function acceptPickDrafts(
	picks: readonly DesignPick[],
	limits: AttachmentLimits,
	staged: readonly NewAttachment[] = [],
): { ok: true; drafts: NewAttachment[] } | { ok: false; message: string } {
	const drafts: NewAttachment[] = [];
	let current = [...staged];
	for (const [i, pick] of picks.entries()) {
		const text = pickPayload(pick);
		const bytes = new TextEncoder().encode(text);
		const outcome = acceptAttachment(pickFileName(i), bytes, limits, current);
		if (!outcome.ok) return { ok: false, message: outcome.refusal.message };
		drafts.push(outcome.draft);
		current = [...current, outcome.draft];
	}
	return { ok: true, drafts };
}

export function applyPicks(
	existing: readonly DesignPick[],
	next: DesignPick,
	mode: PickMode,
): DesignPick[] {
	return mode === "replace" ? [next] : [...existing, next];
}

export function cropFromImage(img: HTMLImageElement, box: CssBox): string {
	const canvas = document.createElement("canvas");
	const width = Math.max(1, Math.round(box.width));
	const height = Math.max(1, Math.round(box.height));
	canvas.width = width;
	canvas.height = height;
	const ctx = canvas.getContext("2d");
	if (ctx === null) return "";
	ctx.drawImage(img, box.x, box.y, box.width, box.height, 0, 0, width, height);
	return canvas.toDataURL("image/png");
}
