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
import { readFileSync, readdirSync } from "node:fs";
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

// The gate is only the first place an event can die. Passing it and then
// reaching a sink no store reduces is the same dead feature with a longer
// walk: `checkpoint_notice` did exactly that for three milestones — the
// daemon raised it from two call sites, `client.ts` waved it through, and
// nothing in the app ever looked at it, so a user whose workspace was not a
// git repository was never told checkpoints were off.
describe("every declared event reaches something", () => {
	/**
	 * Every app source that could *read* an event.
	 *
	 * `protocol.ts` is excluded because declaring the type is the thing
	 * being audited. `client.ts` is included but with `KNOWN_EVENT_TYPES`
	 * cut out — that array names every event by construction, so leaving it
	 * in makes this whole check vacuous. What remains of `client.ts` still
	 * counts as a reader: `log_trimmed` is consumed entirely by the cursor
	 * jump in `dispatch` and has nothing left for a store to do.
	 */
	function readerSources(): string[] {
		const out: string[] = [];
		const walk = (dir: string): void => {
			for (const entry of readdirSync(dir, { withFileTypes: true })) {
				const full = `${dir}/${entry.name}`;
				if (entry.isDirectory()) {
					walk(full);
					continue;
				}
				if (!/\.(ts|svelte)$/.test(entry.name)) continue;
				if (entry.name === "protocol.ts" || entry.name.includes(".test.")) continue;
				if (entry.name.endsWith(".d.ts")) continue;
				out.push(entry.name === "client.ts" ? withoutGateList(full) : readFileSync(full, "utf-8"));
			}
		};
		walk(resolve(process.cwd(), "src"));
		return out;
	}

	function withoutGateList(path: string): string {
		const text = readFileSync(path, "utf-8");
		const start = text.indexOf("const KNOWN_EVENT_TYPES");
		if (start < 0) throw new Error("KNOWN_EVENT_TYPES not found in client.ts");
		const end = text.indexOf("]", start);
		if (end < 0) throw new Error("KNOWN_EVENT_TYPES is not a closed array literal");
		return text.slice(0, start) + text.slice(end);
	}

	it("cut the gate list out, so this check cannot pass on the gate alone", () => {
		const client = readerSources().find((src) => src.includes("KNOWN_EVENT_TYPES")) ?? "";
		expect(client).not.toBe("");
		// Pick an event that only the gate mentions in client.ts.
		expect(client).not.toContain('"conversation_reset"');
	});

	it("finds a reader for every DaemonEventUnion member", () => {
		const blob = readerSources().join("\n");
		expect(blob.length).toBeGreaterThan(1000);
		const unread = declaredEventTypes().filter(
			(type) => !blob.includes(`"${type}"`) && !blob.includes(`'${type}'`),
		);
		expect(unread).toEqual([]);
	});
});
