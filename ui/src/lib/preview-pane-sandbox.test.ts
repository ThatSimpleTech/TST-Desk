// @vitest-environment jsdom
//
// TD-5266: the Grok preview's frame really carries its sandbox once the
// component is on the page. A source check is not enough: Svelte can drop
// an empty attribute when it is written as an expression.

import { afterEach, describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import PreviewPane from "./components/PreviewPane.svelte";
import { grok } from "./grok.svelte.js";

let app: ReturnType<typeof mount> | null = null;

afterEach(async () => {
	if (app !== null) await unmount(app);
	app = null;
	document.body.replaceChildren();
	grok.preview = null;
});

describe("the Grok preview frame", () => {
	it.each(["url", "html", "pdf"] as const)("is sandboxed with nothing allowed for a %s", (kind) => {
		grok.preview = {
			kind,
			title: "Page",
			url: "http://127.0.0.1:5173/",
			path: null,
		} as unknown as typeof grok.preview;
		app = mount(PreviewPane, { target: document.body });
		flushSync();
		const frame = document.querySelector("iframe");
		expect(frame).not.toBeNull();
		expect(frame!.hasAttribute("sandbox")).toBe(true);
		expect(frame!.getAttribute("sandbox")).toBe("");
	});
});
