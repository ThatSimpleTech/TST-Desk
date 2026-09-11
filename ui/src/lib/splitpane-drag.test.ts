// @vitest-environment jsdom
//
// TD-1011: a drag that leaves the 4px divider must still resize and must
// still end. The production bug bound move/up to the divider and relied
// on pointer capture; WKWebView does not always honour that, so the
// pointerup landed elsewhere, `dragging` latched, and the pane froze.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import SplitPaneHarness from "./components/SplitPane.harness.svelte";

let app: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	// jsdom's localStorage is not always installed in this vitest
	// environment (Node warns about --localstorage-file). The persist
	// path is not what this test is for; stub the store so the $effect
	// and the up-handler can run.
	const data = new Map<string, string>();
	const storage: Storage = {
		getItem: (key) => data.get(key) ?? null,
		setItem: (key, value) => void data.set(key, value),
		removeItem: (key) => void data.delete(key),
		clear: () => data.clear(),
		key: (index) => [...data.keys()][index] ?? null,
		get length() {
			return data.size;
		},
	};
	Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
	// bind:clientWidth needs ResizeObserver; jsdom has none. The drag
	// path reads getBoundingClientRect (stubbed below), so observe is a no-op.
	if (typeof globalThis.ResizeObserver === "undefined") {
		globalThis.ResizeObserver = class {
			observe(): void {}
			unobserve(): void {}
			disconnect(): void {}
		} as unknown as typeof ResizeObserver;
	}
});

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
});

function pane(): HTMLElement {
	const el = document.body.querySelector(".splitpane");
	if (!(el instanceof HTMLElement)) throw new Error("splitpane not mounted");
	return el;
}

function divider(): HTMLElement {
	const el = document.body.querySelector('[role="separator"]');
	if (!(el instanceof HTMLElement)) throw new Error("divider not mounted");
	return el;
}

function dispatch(target: EventTarget, type: string, clientX: number): void {
	target.dispatchEvent(
		new PointerEvent(type, {
			bubbles: true,
			clientX,
			clientY: 50,
			pointerId: 1,
			pointerType: "mouse",
		}),
	);
}

describe("split divider drag (TD-1011)", () => {
	it("pointerdown on the divider, then move and up outside it, resizes and clears dragging", async () => {
		// jsdom does not layout, so getBoundingClientRect is 0×0 unless we
		// stub it. 1000px wide matches the harness frame; the right pane
		// width is container.right - pointer x.
		app = mount(SplitPaneHarness, { target: document.body });
		await tick();
		const root = pane();
		root.getBoundingClientRect = () =>
			({
				left: 0,
				width: 1000,
				top: 0,
				height: 200,
				right: 1000,
				bottom: 200,
				x: 0,
				y: 0,
				toJSON: () => ({}),
			}) as DOMRect;

		const start = Number(root.dataset.rightPx);
		expect(root.dataset.dragging).toBe("false");

		dispatch(divider(), "pointerdown", 400);
		await tick();
		expect(root.dataset.dragging).toBe("true");

		// Move and release on window — the whole point of the story. A
		// listener still bound to the divider would see neither event.
		dispatch(window, "pointermove", 700);
		await tick();
		dispatch(window, "pointerup", 700);
		await tick();

		expect(root.dataset.dragging).toBe("false");
		expect(Number(root.dataset.rightPx)).toBe(300);
		expect(Number(root.dataset.rightPx)).not.toBe(start);
	});
});
