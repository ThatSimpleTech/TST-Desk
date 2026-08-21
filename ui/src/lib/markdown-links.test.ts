// @vitest-environment jsdom
//
// TD-4806: a click on a model-emitted link must reach the Tauri opener
// bridge, never webview navigation. Mounts the real Markdown component,
// clicks the rendered anchor, and asserts on the opener mock plus
// defaultPrevented.

import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Markdown from "./components/chat/Markdown.svelte";

const openUrl = vi.fn<(url: string) => Promise<void>>().mockResolvedValue(undefined);

vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl }));

let app: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
	openUrl.mockClear();
});

async function render(text: string): Promise<HTMLElement> {
	app = mount(Markdown, { target: document.body, props: { text } });
	await tick();
	const el = document.body.querySelector(".markdown");
	if (!(el instanceof HTMLElement)) throw new Error("markdown did not mount");
	return el;
}

describe("Markdown link clicks (TD-4806)", () => {
	it("routes an anchor click to the opener, not webview navigation", async () => {
		const root = await render("[docs](https://example.com)");
		const anchor = root.querySelector("a");
		if (!(anchor instanceof HTMLAnchorElement)) throw new Error("anchor not rendered");

		const click = new MouseEvent("click", { bubbles: true, cancelable: true });
		anchor.dispatchEvent(click);
		await vi.waitFor(() => expect(openUrl).toHaveBeenCalledWith("https://example.com"));
		expect(click.defaultPrevented).toBe(true);
	});

	it("an inert (scheme-dropped) link reaches neither the opener nor navigation", async () => {
		const root = await render("[click](javascript:alert(1))");
		const anchor = root.querySelector("a");
		if (!(anchor instanceof HTMLAnchorElement)) throw new Error("anchor not rendered");
		expect(anchor.getAttribute("href")).toBeNull();

		const click = new MouseEvent("click", { bubbles: true, cancelable: true });
		anchor.dispatchEvent(click);
		await tick();
		expect(openUrl).not.toHaveBeenCalled();
		expect(click.defaultPrevented).toBe(false);
	});

	it("copy buttons still work alongside link routing", async () => {
		const root = await render("```\nplain\n```");
		const btn = root.querySelector("[data-copy-btn]");
		if (!(btn instanceof HTMLButtonElement)) throw new Error("copy button not rendered");
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

		btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
		await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("plain"));
		expect(openUrl).not.toHaveBeenCalled();
	});
});
