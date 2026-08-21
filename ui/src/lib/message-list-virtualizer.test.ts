// @vitest-environment jsdom
//
// TD-1012: MessageList's options $effect used to read `$virtualizer` and
// then call `setOptions`, which notifies that store and re-triggers the
// effect until Svelte throws `effect_update_depth_exceeded`. The throw
// escapes the runtime and freezes the whole window.

import { afterEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import MessageListHarness from "./components/chat/MessageList.harness.svelte";

let app: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	document.body.replaceChildren();
});

async function startHarness(): Promise<{
	seed: (n: number) => void;
	append: (t: string) => void;
}> {
	let api: { seed: (n: number) => void; append: (t: string) => void } | null = null;
	app = mount(MessageListHarness, {
		target: document.body,
		props: {
			onready: (next: { seed: (n: number) => void; append: (t: string) => void }) => (api = next),
		},
	});
	await tick();
	if (api === null) throw new Error("harness did not start");
	return api;
}

function sizerHeight(): string {
	const el = document.body.querySelector(".sizer");
	if (!(el instanceof HTMLElement)) throw new Error("sizer not mounted");
	return el.style.height;
}

describe("message list virtualizer (TD-1012)", () => {
	it("mounting a replayed conversation does not throw effect_update_depth_exceeded", async () => {
		const api = await startHarness();
		// The looping effect threw out of the runtime on the first flush
		// with a non-empty conversation. Ten ticks is well past where it
		// used to give up.
		api.seed(40);
		for (let i = 0; i < 10; i++) await tick();
		expect(document.body.querySelector(".list-wrap")).not.toBeNull();
	});

	it("re-sets options as the conversation grows, without running unboundedly", async () => {
		const api = await startHarness();
		api.seed(2);
		await tick();
		const before = sizerHeight();
		api.append("three");
		api.append("four");
		api.append("five");
		for (let i = 0; i < 10; i++) await tick();
		// Still tracking: the virtualizer's sizer grew. A broken fix that
		// deleted the effect would leave the height stuck at the seed.
		expect(sizerHeight()).not.toBe(before);
		expect(document.body.querySelector(".list-wrap")).not.toBeNull();
	});
});
