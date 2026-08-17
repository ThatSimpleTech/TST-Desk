// The client's event gate must know every event the daemon can send (TD-1010).
//
// `ProtocolClient.dispatch` drops any frame whose type is absent from
// `KNOWN_EVENT_TYPES` — before `sink.onEvent`, with only a console warning.
// So a new daemon event that reaches `DaemonEventUnion` but not that set is
// invisible at runtime and green in every unit test, because the stores are
// tested by calling their reducers directly.
//
// It has happened twice. `policy_rules` carries the scar in a comment
// ("TD-803: was missing; settings events arrived as unknown"), and TD-1706's
// usage panel shipped unable to receive either of its own events — it would
// have sat on `loading` forever.
//
// TypeScript cannot enumerate a union at runtime, so the check reads the two
// sources and compares them. Crude, and it is the only thing that closes the
// gap from this side.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Same shape as timeline-bench.test.ts and tokens.test.ts: the app tree is
// browser-typed, @types/node is deliberately absent, so filesystem tests use
// the ambient shims in node-test-shims.d.ts.
const read = (rel: string): string => readFileSync(resolve(process.cwd(), rel), "utf-8");

const CLIENT = read("src/lib/client.ts");
const PROTOCOL = read("src/lib/protocol.ts");

/** The `type` literal of every interface named in `DaemonEventUnion`. */
function declaredEventTypes(): string[] {
	const start = PROTOCOL.indexOf("export type DaemonEventUnion");
	if (start < 0) throw new Error("DaemonEventUnion not found in protocol.ts");
	const union = PROTOCOL.slice(start, PROTOCOL.indexOf(";", start));
	const members = union
		.slice(union.indexOf("=") + 1)
		.split("|")
		.map((m) => m.trim())
		.filter(Boolean);

	return members.map((name) => {
		const re = new RegExp(`interface ${name} extends DaemonEvent \\{\\s*type: "([a-z_]+)"`);
		const hit = re.exec(PROTOCOL);
		if (hit === null) throw new Error(`no type literal for DaemonEventUnion member ${name}`);
		return hit[1];
	});
}

/** The contents of the client's `KNOWN_EVENT_TYPES` set. */
function gatedEventTypes(): string[] {
	const start = CLIENT.indexOf("const KNOWN_EVENT_TYPES");
	if (start < 0) throw new Error("KNOWN_EVENT_TYPES not found in client.ts");
	const body = CLIENT.slice(start, CLIENT.indexOf("]", start));
	return [...body.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

describe("the client's event gate", () => {
	const declared = declaredEventTypes();
	const gated = gatedEventTypes();

	it("parsed both sides, so a broken parse cannot pass this file", () => {
		expect(declared.length).toBeGreaterThan(15);
		expect(gated.length).toBeGreaterThan(15);
	});

	it("accepts every event declared in DaemonEventUnion", () => {
		// Failing here means the daemon can send something the client silently
		// drops: the feature is dead at runtime and green in every unit test.
		expect(new Set(gated)).toEqual(new Set(declared));
	});

	it("gates nothing that is not a declared event", () => {
		// The other direction: a stale entry is a client message or a removed
		// event pretending to still exist.
		expect(gated.filter((t) => !declared.includes(t))).toEqual([]);
	});

	it("still drops an unknown frame rather than forwarding it", () => {
		// The gate exists for a reason — this asserts the fix did not become
		// "accept everything", which would satisfy the tests above trivially.
		expect(CLIENT).toContain("ignored unknown event type");
		expect(CLIENT).toMatch(/if \(!known\) \{/);
	});
});
