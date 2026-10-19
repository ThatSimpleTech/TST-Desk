// @vitest-environment jsdom
//
// TD-5109: the title-bar gauge renders the daemon's last context_usage,
// hides until there is one, and its Compact now button asks the chat
// store, standing down while a turn is live. The chat store is mocked so
// no socket comes along.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	compactContext: vi.fn<(guidance?: string, userIndex?: number) => boolean>(() => true),
	chat: { turnState: "idle" as string | null },
}));

vi.mock("./chat-store.svelte.js", () => ({
	chat: mocks.chat,
	compactContext: mocks.compactContext,
}));

import ContextGauge from "./components/ContextGauge.svelte";
import { ingestEvent, resetSession } from "./session-status.svelte.js";

function adopt(): void {
	ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 } as DaemonEventUnion);
}

function usage(used: number): void {
	ingestEvent({
		type: "context_usage",
		session_id: "s1",
		tier: "brain",
		used_tokens: used,
		context_window: 65536,
		compact_at: 49152,
		seq: 2,
	} as DaemonEventUnion);
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	resetSession();
	mocks.compactContext.mockClear();
	mocks.chat.turnState = "idle";
	target = document.createElement("div");
	document.body.appendChild(target);
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
});

function render(): void {
	component = mount(ContextGauge, { target });
	flushSync();
}

describe("ContextGauge", () => {
	it("stays hidden until the loop has measured a request", () => {
		adopt();
		render();
		expect(target.querySelector(".gauge")).toBeNull();
	});

	it("shows used against the window, and the trigger point", () => {
		adopt();
		usage(40_000);
		render();
		const gauge = target.querySelector(".gauge");
		expect(gauge).not.toBeNull();
		expect(gauge!.classList.contains("gauge--near")).toBe(true);
		expect(target.querySelector(".label")!.textContent).toContain("40k / 64k");
		expect(target.textContent).toContain("61%");
		expect(target.textContent).toContain((49152).toLocaleString());
	});

	it("compacts on click, and not while a turn is live", () => {
		adopt();
		usage(1000);
		render();
		const button = [...target.querySelectorAll("button")].find((b) =>
			b.textContent?.includes("Compact now"),
		)!;
		button.click();
		expect(mocks.compactContext).toHaveBeenCalledTimes(1);

		unmount(component!);
		mocks.chat.turnState = "running";
		render();
		const live = [...target.querySelectorAll("button")].find((b) =>
			b.textContent?.includes("Compact now"),
		)!;
		expect(live.disabled).toBe(true);
	});
});
