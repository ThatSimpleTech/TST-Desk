// @vitest-environment jsdom
//
// TD-5113: the trust card lists every setting a workspace config loosens,
// and answers with the hash it showed. A decline leaves a one-line notice
// that opens back into the card.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ProtocolClient } from "./client";
import type { DaemonEventUnion } from "./protocol";

import WorkspaceTrustCard from "./components/WorkspaceTrustCard.svelte";
import { bindClient, ingestEvent, resetSession } from "./session-status.svelte.js";

const SHA = "c".repeat(64);

function wall(state: "pending" | "trusted" | "declined" | null): void {
	ingestEvent({
		type: "boundary_update",
		session_id: "s1",
		seq: 2,
		writable_paths: ["**"],
		allowed_commands: [],
		network: "deny",
		spend_usd: 25,
		wall_clock_hours: 8,
		max_iterations: 200,
		source: "defaults — workspace config not trusted yet",
		config_trust:
			state === null
				? null
				: {
						state,
						sha256: SHA,
						loosened: [
							{ key: "policy.rules", value: "shell: touch * → auto", default: "ask (class B and C)" },
							{ key: "boundary.network", value: "example.com", default: "deny" },
						],
					},
	} as DaemonEventUnion);
	flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let sent: unknown[];

beforeEach(() => {
	resetSession();
	sent = [];
	bindClient({
		send: (m: unknown) => (sent.push(m), true),
		attach: () => undefined,
	} as unknown as ProtocolClient);
	ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 } as DaemonEventUnion);
	target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(WorkspaceTrustCard, { target });
	flushSync();
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
	bindClient(null);
});

function button(label: string): HTMLButtonElement | undefined {
	return [...target.querySelectorAll("button")].find((b) => b.textContent?.includes(label));
}

describe("WorkspaceTrustCard", () => {
	it("shows nothing when there is nothing to ask, or the config is trusted", () => {
		expect(target.querySelector(".trust-bar")).toBeNull();
		wall(null);
		expect(target.querySelector(".trust-bar")).toBeNull();
		wall("trusted");
		expect(target.querySelector(".trust-bar")).toBeNull();
	});

	it("lists each loosened setting with its default and trusts that exact file", () => {
		wall("pending");
		const rules = [...target.querySelectorAll(".rule")].map((r) => r.textContent ?? "");
		expect(rules).toHaveLength(2);
		expect(rules[0]).toContain("policy.rules");
		expect(rules[0]).toContain("shell: touch * → auto");
		expect(rules[1]).toContain("default: deny");
		button("Trust and apply")!.click();
		expect(sent).toEqual([
			{ type: "trust_workspace_config", session_id: "s1", sha256: SHA, trust: true },
		]);
	});

	it("keeps the defaults on decline, then offers a review", () => {
		wall("pending");
		button("Keep defaults")!.click();
		expect(sent).toEqual([
			{ type: "trust_workspace_config", session_id: "s1", sha256: SHA, trust: false },
		]);
		wall("declined");
		expect(target.querySelector(".rule")).toBeNull();
		expect(target.textContent).toContain("You kept the defaults");
		button("Review")!.click();
		flushSync();
		expect(target.querySelectorAll(".rule")).toHaveLength(2);
		expect(button("Trust and apply")).toBeDefined();
	});
});
