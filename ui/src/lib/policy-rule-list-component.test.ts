// @vitest-environment jsdom
//
// TD-5149: Always allow saves the exact path with `*`, `?` and `[` escaped, so
// Settings → Policy shows the path the rule was for, not the escaped pattern,
// and a glob still shows as written. Revoke sends the raw identity.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[] }));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: () => () => undefined,
	sendToDaemon: (msg: ClientMessageUnion) => (mocks.sent.push(msg), true),
}));

import PolicyRuleList from "./components/PolicyRuleList.svelte";
import { ARM_MS } from "./approval-arming";
import { settings } from "./settings.svelte.js";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

// A destructive row action asks first and arms after ARM_MS (INTERACT-12).
async function confirmRow(root: ParentNode): Promise<void> {
	await new Promise((r) => setTimeout(r, ARM_MS + 20));
	flushSync();
	const group = root.querySelector('[role="group"]');
	const yes = group?.querySelector("button");
	if (!(yes instanceof HTMLButtonElement)) throw new Error("no confirmation asked");
	yes.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
	yes.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
	flushSync();
}

beforeEach(() => {
	mocks.sent = [];
	settings.rulesSessionId = "s1";
	settings.rules = [
		{ tool: "fs_read", args: "app/[[]id]/page.tsx", effect: "auto", exact: "app/[id]/page.tsx" },
		{ tool: "fs_read", args: "*.env*", effect: "never", exact: null },
	];
	target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(PolicyRuleList, { target, props: { sessionId: "s1" } });
	flushSync();
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
	settings.rules = [];
	settings.rulesSessionId = null;
});

describe("PolicyRuleList", () => {
	it("shows an escaped rule as its path and a glob as written", () => {
		const args = [...target.querySelectorAll(".rule-args")].map((el) => el.textContent);
		expect(args).toEqual(["exactly app/[id]/page.tsx", "*.env*"]);
		// The raw pattern, as it sits in .tst/config.yaml, is one hover away.
		expect(target.querySelector(".rule-args")?.getAttribute("title")).toBe("app/[[]id]/page.tsx");
		const revoke = target.querySelector("button.revoke");
		expect(revoke?.getAttribute("aria-label")).toBe("Revoke fs_read exactly app/[id]/page.tsx");
	});

	it("revokes by the saved pattern, not the text it shows", async () => {
		(target.querySelector("button.revoke") as HTMLButtonElement).click();
		flushSync();
		expect(mocks.sent).toEqual([]);
		await confirmRow(target);
		expect(mocks.sent).toEqual([
			{ type: "revoke_policy_rule", session_id: "s1", tool: "fs_read", args: "app/[[]id]/page.tsx" },
		]);
	});
});
