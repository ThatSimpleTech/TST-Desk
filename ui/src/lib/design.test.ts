import { describe, expect, it } from "vitest";
import { DEFAULT_ATTACHMENT_LIMITS, acceptAttachment, isTextBytes } from "./attachments";
import {
	acceptPickDrafts,
	applyPicks,
	canRunDesign,
	clampBox,
	containedDrawRect,
	geometricHitNode,
	mapClientToFrame,
	movedEnough,
	pickLabel,
	pickPayload,
	pickToDraft,
	picksToDrafts,
	rectFromPoints,
	scriptedHitNode,
	type DesignPick,
} from "./design";

const CROP = "data:image/png;base64,aaa";

function pick(over: Partial<DesignPick> = {}): DesignPick {
	return {
		id: "pick-1",
		xpath: "//*[@id='ok']",
		role: "button",
		attributes: { id: "ok" },
		box: { x: 10, y: 20, width: 80, height: 24 },
		styles: { display: "inline-block" },
		cropDataUrl: CROP,
		...over,
	};
}

describe("canRunDesign", () => {
	it("is on only with a frozen frame and no actuation", () => {
		expect(canRunDesign({ actuating: false, hasPreview: true })).toBe(true);
		expect(canRunDesign({ actuating: true, hasPreview: true })).toBe(false);
		expect(canRunDesign({ actuating: false, hasPreview: false })).toBe(false);
	});
});

describe("mapClientToFrame", () => {
	it("maps a click through object-fit: contain letterboxing", () => {
		// 200×100 element holding a 100×100 frame: 50px gutters left/right.
		const mapped = mapClientToFrame(80, 40, { left: 0, top: 0, width: 200, height: 100 }, 100, 100);
		expect(mapped).toEqual({ x: 30, y: 40 });
	});

	it("ignores a click in the letterbox", () => {
		expect(
			mapClientToFrame(10, 40, { left: 0, top: 0, width: 200, height: 100 }, 100, 100),
		).toBeNull();
	});
});

describe("containedDrawRect", () => {
	it("letterboxes a square frame in a wide pane", () => {
		const draw = containedDrawRect(200, 100, 100, 100);
		expect(draw).toEqual({ left: 50, top: 0, width: 100, height: 100, scale: 1 });
	});
});

describe("click / shift-click / shift-drag", () => {
	it("click replaces; shift-click adds", () => {
		const first = pick({ id: "a" });
		const second = pick({ id: "b", xpath: "//div" });
		expect(applyPicks([], first, "replace")).toEqual([first]);
		expect(applyPicks([first], second, "replace")).toEqual([second]);
		expect(applyPicks([first], second, "add")).toEqual([first, second]);
	});

	it("shift-drag builds a region box from two points", () => {
		expect(rectFromPoints({ x: 80, y: 40 }, { x: 10, y: 90 })).toEqual({
			x: 10,
			y: 40,
			width: 70,
			height: 50,
		});
		expect(movedEnough({ x: 0, y: 0 }, { x: 3, y: 0 })).toBe(false);
		expect(movedEnough({ x: 0, y: 0 }, { x: 4, y: 0 })).toBe(true);
	});
});

describe("hit nodes", () => {
	it("scripted node matches the mock driver (xpath + role + box)", () => {
		const node = scriptedHitNode(12, 34);
		expect(node.xpath).toBe("//*[@data-mock-point='12,34']");
		expect(node.role).toBe("button");
		expect(node.box).toEqual({ x: -8, y: 24, width: 80, height: 24 });
		expect(node.attributes.id).toBe("mock-target");
	});

	it("desktop geometric picks have no xpath or AX role", () => {
		const node = geometricHitNode({ x: 1, y: 2, width: 3, height: 4 });
		expect(node.xpath).toBeNull();
		expect(node.role).toBeNull();
		expect(node.box.width).toBe(3);
	});
});

describe("chip payload travels as a text attachment", () => {
	it("JSON sidecar carries xpath, attributes, box, styles, and the crop", () => {
		const body = JSON.parse(pickPayload(pick())) as {
			kind: string;
			xpath: string;
			role: string;
			attributes: Record<string, string>;
			box: { width: number };
			styles: Record<string, string>;
			crop_data_url: string;
		};
		expect(body.kind).toBe("design_pick");
		expect(body.xpath).toBe("//*[@id='ok']");
		expect(body.role).toBe("button");
		expect(body.attributes.id).toBe("ok");
		expect(body.box.width).toBe(80);
		expect(body.styles.display).toBe("inline-block");
		expect(body.crop_data_url).toBe(CROP);
	});

	it("is UTF-8 text, so TD-1709 caps accept it", () => {
		const draft = pickToDraft(pick(), 0);
		expect(draft.name).toBe("design-pick.json");
		const bytes = Uint8Array.from(atob(draft.content_b64), (c) => c.charCodeAt(0));
		expect(isTextBytes(bytes)).toBe(true);
		const outcome = acceptAttachment(draft.name, bytes, DEFAULT_ATTACHMENT_LIMITS);
		expect(outcome.ok).toBe(true);
	});

	it("a second pick names itself design-pick-2.json", () => {
		expect(picksToDrafts([pick(), pick({ id: "2" })]).map((d) => d.name)).toEqual([
			"design-pick.json",
			"design-pick-2.json",
		]);
	});

	it("refuses when the message is already at max_count", () => {
		const staged = [{ name: "a.txt", size: 1, content_b64: "eA==" }];
		const result = acceptPickDrafts([pick()], { ...DEFAULT_ATTACHMENT_LIMITS, max_count: 1 }, staged);
		expect(result.ok).toBe(false);
	});
});

describe("helpers", () => {
	it("labels the chip with xpath, then role", () => {
		expect(pickLabel(pick())).toBe("//*[@id='ok']");
		expect(pickLabel(pick({ xpath: null }))).toBe("button");
		expect(pickLabel(pick({ xpath: null, role: null }))).toBe("design pick");
	});

	it("clamps a box to the frame", () => {
		expect(clampBox({ x: -4, y: 90, width: 40, height: 40 }, { width: 100, height: 100 })).toEqual({
			x: 0,
			y: 90,
			width: 40,
			height: 10,
		});
	});
});
