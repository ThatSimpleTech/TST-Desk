// @vitest-environment jsdom
//
// TD-5197: the daemon refreshes model cards in the background, so the Models
// page asks for fresh numbers each time it opens instead of showing the ones
// the window read when it connected.

import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => undefined,
	onDaemonEvent: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import SettingsModels from "./components/SettingsModels.svelte";

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = "";
	mocks.sent = [];
});

describe("Settings → Model", () => {
	it("asks the daemon for fresh numbers when it opens", () => {
		const target = document.createElement("div");
		document.body.appendChild(target);
		component = mount(SettingsModels, { target });
		flushSync();
		expect(mocks.sent).toContainEqual({ type: "get_setup_state" });
	});
});
