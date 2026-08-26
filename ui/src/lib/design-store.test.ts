import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import { pickToDraft, scriptedHitNode, type DesignPick } from "./design";
import { screen, setScreenPreviewReader, startScreen, resetScreen } from "./screen.svelte.js";
import { session } from "./session-status.svelte.js";

const mocks = vi.hoisted(() => ({
	handlers: new Set<(e: DaemonEventUnion) => void>(),
	sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handlers.add(handler);
		return () => {
			mocks.handlers.delete(handler);
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
}));

import {
	addPick,
	clearPicks,
	design,
	resetDesign,
	setDesignHitTester,
	startDesign,
	toggleDesign,
} from "./design.svelte.js";

const CROP = "data:image/png;base64,aaa";

function emit(event: DaemonEventUnion): void {
	for (const handler of mocks.handlers) handler(event);
}

async function freezePreview(): Promise<void> {
	setScreenPreviewReader(async () => "data:image/png;base64,frame");
	emit({
		type: "screen_frame",
		seq: 1,
		session_id: "s1",
		path: "screens/aa.png",
		mime: "image/png",
		width: 100,
		height: 80,
	});
	await vi.waitFor(() => {
		expect(screen.preview).toBe("data:image/png;base64,frame");
	});
}

describe("design store (TD-3403)", () => {
	beforeEach(() => {
		mocks.sent = [];
		resetScreen();
		startScreen();
		resetDesign();
		startDesign();
		setDesignHitTester((x, y) => scriptedHitNode(x, y));
	});

	afterEach(() => {
		session.sessionId = null;
		resetDesign();
		resetScreen();
	});

	it("⌘⇧D toggles Design on a frozen frame; off is the watch surface", async () => {
		expect(toggleDesign()).toBe(false);
		await freezePreview();
		expect(toggleDesign()).toBe(true);
		expect(design.enabled).toBe(true);
		expect(design.frozenPreview).toBe("data:image/png;base64,frame");
		expect(toggleDesign()).toBe(false);
		expect(design.enabled).toBe(false);
	});

	it("click / shift-click / shift-drag select on the frozen frame", async () => {
		await freezePreview();
		toggleDesign();
		const click = await addPick({ x: 12, y: 34, cropDataUrl: CROP, mode: "replace" });
		expect(click?.xpath).toBe("//*[@data-mock-point='12,34']");
		expect(design.picks).toHaveLength(1);
		await addPick({
			x: 40,
			y: 10,
			box: { x: 10, y: 8, width: 40, height: 20 },
			cropDataUrl: CROP,
			mode: "add",
		});
		expect(design.picks).toHaveLength(2);
		expect(design.picks[1]?.box).toEqual({ x: 10, y: 8, width: 40, height: 20 });
	});

	it("chip travels with user_message under attachment caps", async () => {
		await freezePreview();
		toggleDesign();
		const made = await addPick({ x: 12, y: 34, cropDataUrl: CROP, mode: "replace" });
		expect(made).not.toBeNull();
		const drafts = design.picks.map((p: DesignPick, i: number) => pickToDraft(p, i));
		const sent: ClientMessageUnion[] = [];
		const deps: ChatDeps = {
			send: (msg) => {
				sent.push(msg);
				return true;
			},
			attach: () => {},
			detach: () => {},
		};
		const store = createChatStore(deps, createChatState());
		store.applyEvent({
			type: "session_list",
			seq: 1,
			sessions: [
				{
					session_id: "s1",
					workspace_path: "/ws",
					state: "idle",
					created_at: "2026-08-21T00:00:00Z",
					updated_at: "2026-08-21T00:00:00Z",
					event_count: 0,
					archived: false,
					starred: false,
				},
			],
		});
		expect(store.sendUserMessage("look at this", drafts)).toBe(true);
		expect(sent[0]).toMatchObject({
			type: "user_message",
			session_id: "s1",
			content: "look at this",
		});
		const attachments = sent[0] && "attachments" in sent[0] ? sent[0].attachments : undefined;
		expect(attachments?.[0]?.name).toBe("design-pick.json");
		const text = atob(attachments?.[0]?.content_b64 ?? "");
		const body = JSON.parse(text) as { xpath: string; crop_data_url: string };
		expect(body.xpath).toBe("//*[@data-mock-point='12,34']");
		expect(body.crop_data_url).toBe(CROP);
		clearPicks();
		expect(design.picks).toEqual([]);
	});

	it("cannot run while the agent is actuating that surface", async () => {
		await freezePreview();
		expect(toggleDesign()).toBe(true);
		emit({
			type: "tool_call",
			seq: 2,
			session_id: "s1",
			tool_call_id: "c1",
			name: "browser_click",
			arguments: { x: 1, y: 1 },
		});
		expect(design.actuating).toBe(true);
		expect(design.enabled).toBe(false);
		expect(toggleDesign()).toBe(false);
		expect(await addPick({ x: 1, y: 1, cropDataUrl: CROP, mode: "replace" })).toBeNull();
		emit({
			type: "tool_result",
			seq: 3,
			session_id: "s1",
			tool_call_id: "c1",
			status: "success",
			output: "ok",
			truncated: false,
		});
		expect(design.actuating).toBe(false);
		expect(toggleDesign()).toBe(true);
	});

	it("desktop frames send design_hit_test and enrich an AX chip (TD-3406)", async () => {
		await freezePreview();
		toggleDesign();
		session.sessionId = "s1";
		emit({
			type: "tool_call",
			seq: 2,
			session_id: "s1",
			tool_call_id: "c1",
			name: "desktop_screenshot",
			arguments: {},
		});
		expect(design.surface).toBe("desktop");
		expect(design.enabled).toBe(true);
		setDesignHitTester(null);
		const made = await addPick({ x: 12, y: 34, cropDataUrl: CROP, mode: "replace" });
		expect(made?.role).toBeNull();
		expect(mocks.sent).toContainEqual({
			type: "design_hit_test",
			session_id: "s1",
			x: 12,
			y: 34,
		});
		emit({
			type: "design_hit",
			seq: 1,
			session_id: "s1",
			x: 12,
			y: 34,
			xpath: null,
			role: "AXButton",
			attributes: { AXTitle: "Mock", AXIdentifier: "mock-target" },
			box: { x: -8, y: 24, width: 80, height: 24 },
			styles: {},
		});
		expect(design.picks[0]?.role).toBe("AXButton");
		expect(design.picks[0]?.attributes.AXIdentifier).toBe("mock-target");
		session.sessionId = null;
	});
});
