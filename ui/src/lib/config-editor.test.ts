// TD-5203: the pure side of Settings → Configuration. Every kind of node
// gets the control its type calls for; what a person types becomes a value
// or a reason, never a guess; defaults and the search read only what the
// daemon sent.

import { describe, expect, it } from "vitest";
import type { ConfigModelDef, ConfigNode, JsonValue } from "./protocol";
import {
	boundsHint,
	changedCount,
	choicesFor,
	controlFor,
	deepEqual,
	defaultFor,
	dotted,
	entryLabel,
	formatValue,
	getAt,
	isLeaf,
	isModified,
	joinWords,
	newEntry,
	parseJsonText,
	parseNumber,
	parseText,
	readableBytes,
	pathKey,
	splitWords,
	visibleKeys,
} from "./config-editor";

function node(over: Partial<ConfigNode>): ConfigNode {
	return {
		kind: "str",
		ref: null,
		nullable: false,
		title: "",
		description: "",
		unit: "",
		default: null,
		has_default: false,
		editable: true,
		resettable: true,
		multiline: false,
		note: "",
		minimum: null,
		maximum: null,
		exclusive_minimum: null,
		exclusive_maximum: null,
		min_length: null,
		max_length: null,
		choices: [],
		choices_from: [],
		items: null,
		entries: null,
		variants: [],
		addable: true,
		removable: true,
		add_note: "",
		remove_note: "",
		...over,
	};
}

describe("controlFor", () => {
	it.each([
		[node({ kind: "bool" }), "toggle"],
		[node({ kind: "enum", choices: ["model", "extractive"] }), "choice"],
		[node({ kind: "enum", choices: ["a", "b", "c", "d", "e"] }), "select"],
		[node({ kind: "enum", choices: ["starttls", "a-very-long-choice"] }), "select"],
		[node({ kind: "int" }), "number"],
		[node({ kind: "float", nullable: true }), "number"],
		[node({ kind: "str" }), "text"],
		[node({ kind: "str", multiline: true }), "textarea"],
		[node({ kind: "str", choices_from: ["presets"] }), "select"],
		[node({ kind: "list", items: node({ kind: "str" }) }), "list"],
		[node({ kind: "list", items: node({ kind: "enum", choices: ["brain"] }) }), "chips"],
		[node({ kind: "list", items: node({ kind: "object", ref: "HookEntry" }) }), "entries"],
		[node({ kind: "map", entries: node({ kind: "object", ref: "Preset" }) }), "entries"],
		[node({ kind: "object", ref: "WorkflowConfig" }), "group"],
		[node({ kind: "json", nullable: true }), "json"],
		[node({ kind: "union", variants: [node({}), node({ kind: "list" })] }), "union"],
		[node({ kind: "secrets", editable: false }), "secrets"],
	])("%# picks %s", (n, control) => {
		expect(controlFor(n)).toBe(control);
	});

	it("calls groups and entry lists containers, the rest leaves", () => {
		expect(isLeaf(node({ kind: "object", ref: "X" }))).toBe(false);
		expect(isLeaf(node({ kind: "list", items: node({ kind: "object", ref: "X" }) }))).toBe(false);
		expect(isLeaf(node({ kind: "list", items: node({}) }))).toBe(true);
		expect(isLeaf(node({ kind: "secrets" }))).toBe(true);
	});
});

describe("paths", () => {
	const values: Record<string, JsonValue> = {
		presets: { local: { brain: { fallbacks: [{ slug: "b" }] } } },
	};

	it("keys a path the way the daemon's defaults do", () => {
		expect(pathKey(["workflow", "max_parallel"])).toBe('["workflow","max_parallel"]');
		expect(pathKey(["hooks", "after_write", 0])).toBe('["hooks","after_write",0]');
	});

	it("reads through maps and lists, and names a path like the docs", () => {
		const path = ["presets", "local", "brain", "fallbacks", 0, "slug"];
		expect(getAt(values, path)).toBe("b");
		expect(getAt(values, ["presets", "nope", "brain"])).toBeUndefined();
		expect(getAt(values, ["presets", 0])).toBeUndefined();
		expect(dotted(path)).toBe("presets.local.brain.fallbacks[0].slug");
	});

	it("compares values deeply", () => {
		expect(deepEqual({ a: [1, { b: null }] }, { a: [1, { b: null }] })).toBe(true);
		expect(deepEqual({ a: 1 }, { a: 1, b: 2 })).toBe(false);
		expect(deepEqual([1, 2], [2, 1])).toBe(false);
		expect(deepEqual(null, {})).toBe(false);
	});
});

describe("defaults", () => {
	const defaults = { '["search","max_results"]': 8 };

	it("takes a fixed key's default from the daemon, an entry's from its model", () => {
		const max = node({ kind: "int", has_default: true, default: 5 });
		expect(defaultFor(defaults, ["search", "max_results"], max)).toEqual({ has: true, value: 8 });
		expect(defaultFor(defaults, ["presets", "x", "brain", "vision"], node({ kind: "bool", has_default: true, default: false })))
			.toEqual({ has: true, value: false });
		expect(defaultFor(defaults, ["presets", "x", "brain", "base_url"], node({}))).toEqual({ has: false, value: null });
	});

	it("marks a key modified only when it differs from a default it has", () => {
		expect(isModified(3, { has: true, value: 8 })).toBe(true);
		expect(isModified(8, { has: true, value: 8 })).toBe(false);
		expect(isModified(undefined, { has: true, value: null })).toBe(false);
		expect(isModified("x", { has: false, value: null })).toBe(false);
	});

	it("counts a section's changed keys", () => {
		const d = { '["search","max_results"]': 8, '["search","base_url"]': "a", '["session","x"]': 1 };
		expect(changedCount("search", d, { search: { max_results: 3, base_url: "a" } })).toBe(1);
		expect(changedCount("session", d, { session: { x: 1 } })).toBe(0);
	});
});

describe("choices", () => {
	it("offers a literal's choices, or the keys of the map a reference names", () => {
		expect(choicesFor(node({ kind: "enum", choices: ["a", "b"] }), {})).toEqual(["a", "b"]);
		const ref = node({ choices_from: ["presets"] });
		expect(choicesFor(ref, { presets: { budget: {}, local: {} } })).toEqual(["budget", "local"]);
		expect(choicesFor(ref, {})).toEqual([]);
	});
});

describe("parsing what was typed", () => {
	const workers = node({ kind: "int", minimum: 1, maximum: 16 });
	const timeout = node({ kind: "float", exclusive_minimum: 0, nullable: true });

	it("reads numbers within their bounds", () => {
		expect(parseNumber(" 4 ", workers)).toEqual({ ok: true, value: 4 });
		expect(parseNumber("0", workers)).toEqual({ ok: false, error: "Use at least 1." });
		expect(parseNumber("17", workers)).toEqual({ ok: false, error: "Use at most 16." });
		expect(parseNumber("2.5", workers)).toEqual({ ok: false, error: "Use a whole number." });
		expect(parseNumber("abc", workers)).toEqual({ ok: false, error: "That is not a number." });
		expect(parseNumber("", workers)).toEqual({ ok: false, error: "Enter a number." });
		expect(parseNumber("0", timeout)).toEqual({ ok: false, error: "Use more than 0." });
		expect(parseNumber("2.5", timeout)).toEqual({ ok: true, value: 2.5 });
		expect(parseNumber("", timeout)).toEqual({ ok: true, value: null });
	});

	it("says a number's range in words", () => {
		expect(boundsHint(workers)).toBe("1 to 16");
		expect(boundsHint(timeout)).toBe("more than 0");
		expect(boundsHint(node({ kind: "int", minimum: 0 }))).toBe("at least 0");
		expect(boundsHint(node({}))).toBe("");
	});

	it("checks text length", () => {
		const label = node({ min_length: 1, max_length: 4 });
		expect(parseText("ok", label)).toEqual({ ok: true, value: "ok" });
		expect(parseText("", label)).toEqual({ ok: false, error: "This cannot be empty." });
		expect(parseText("too long", label)).toEqual({ ok: false, error: "Use at most 4 characters." });
	});

	it("reads JSON, and empty as not set", () => {
		const body = node({ kind: "json", nullable: true });
		expect(parseJsonText('{"a": [1, false]}', body)).toEqual({ ok: true, value: { a: [1, false] } });
		expect(parseJsonText("  ", body)).toEqual({ ok: true, value: null });
		expect(parseJsonText("{nope", body)).toEqual({ ok: false, error: "That is not valid JSON." });
	});

	it("splits a command line into words and joins it back", () => {
		expect(splitWords(`ruff  format "my file.py" '{files}'`)).toEqual(["ruff", "format", "my file.py", "{files}"]);
		expect(splitWords('a "" b')).toEqual(["a", "", "b"]);
		expect(joinWords(["ruff", "my file.py"])).toBe('ruff "my file.py"');
	});
});

describe("words on screen", () => {
	it("formats values for a default hint", () => {
		expect(formatValue(null)).toBe("not set");
		expect(formatValue(true)).toBe("on");
		expect(formatValue("")).toBe("empty");
		expect(formatValue([])).toBe("none");
		expect(formatValue(["a", "b"])).toBe("a, b");
		expect(formatValue({ a: 1 })).toBe('{"a":1}');
		expect(formatValue(900)).toBe("900");
		expect(formatValue(536870912, "bytes")).toBe("536870912 (512 MiB)");
		expect(formatValue(1000, "bytes")).toBe("1000");
		expect(readableBytes(4294967296)).toBe("4 GiB");
		expect(readableBytes(1536)).toBe("1.5 KiB");
	});

	it("names an entry by what it holds", () => {
		expect(entryLabel({ name: "Ollama", base_url: "x" }, "Item 1")).toBe("Ollama");
		expect(entryLabel({ name: "", command: ["ruff", "format"] }, "Item 1")).toBe("ruff format");
		expect(entryLabel({ command: "make lint" }, "Item 1")).toBe("make lint");
		expect(entryLabel({ base_url: "x" }, "Item 2")).toBe("Item 2");
	});

	it("builds a new entry from the add form's filled fields only", () => {
		const def: ConfigModelDef = {
			name: "AfterWriteHook",
			add_fields: ["name", "command", "match"],
			empty_ok: false,
			fields: [
				{ name: "name", required: false, node: node({}) },
				{ name: "command", required: true, node: node({ kind: "list" }) },
				{ name: "match", required: false, node: node({ kind: "list" }) },
			],
		};
		expect(newEntry(def, { name: "", command: ["ruff"], match: [] })).toEqual({ command: ["ruff"] });
		expect(newEntry(def, { name: "fmt", command: ["ruff"], match: ["*.py"] })).toEqual({
			name: "fmt",
			command: ["ruff"],
			match: ["*.py"],
		});
	});
});

describe("search", () => {
	const defs: Record<string, ConfigModelDef> = {
		ModelConfig: {
			name: "ModelConfig",
			add_fields: [],
			empty_ok: true,
			fields: [
				{ name: "workflow", required: false, node: node({ kind: "object", ref: "WorkflowConfig", title: "Workflow" }) },
				{ name: "hooks", required: false, node: node({ kind: "object", ref: "HooksConfig", title: "Hooks" }) },
			],
		},
		WorkflowConfig: {
			name: "WorkflowConfig",
			add_fields: [],
			empty_ok: true,
			fields: [
				{ name: "max_parallel", required: false, node: node({ kind: "int", description: "Workers running at once." }) },
				{ name: "max_tasks", required: false, node: node({ kind: "int", description: "Most tasks one call may hold." }) },
			],
		},
		HooksConfig: {
			name: "HooksConfig",
			add_fields: [],
			empty_ok: true,
			fields: [
				{
					name: "after_write",
					required: false,
					node: node({ kind: "list", items: node({ kind: "object", ref: "AfterWriteHook" }) }),
				},
			],
		},
		AfterWriteHook: {
			name: "AfterWriteHook",
			add_fields: [],
			empty_ok: false,
			fields: [{ name: "command", required: true, node: node({ kind: "list", description: "Program and arguments." }) }],
		},
	};
	const values = { workflow: { max_parallel: 3, max_tasks: 8 }, hooks: { after_write: [{ command: ["ruff"] }] } };

	it("shows nothing special without a query", () => {
		expect(visibleKeys("  ", "ModelConfig", defs, values)).toBeNull();
	});

	it("finds a key by name or words and reveals its parents", () => {
		const shown = visibleKeys("workers", "ModelConfig", defs, values);
		expect(shown).toEqual(new Set([pathKey(["workflow"]), pathKey(["workflow", "max_parallel"])]));
	});

	it("shows a whole group when the group matches", () => {
		const shown = visibleKeys("workflow", "ModelConfig", defs, values) ?? new Set();
		expect(shown.has(pathKey(["workflow", "max_tasks"]))).toBe(true);
		expect(shown.has(pathKey(["hooks"]))).toBe(false);
	});

	it("looks inside list items", () => {
		const shown = visibleKeys("program", "ModelConfig", defs, values) ?? new Set();
		expect(shown.has(pathKey(["hooks", "after_write", 0, "command"]))).toBe(true);
		expect(shown.has(pathKey(["hooks", "after_write", 0]))).toBe(true);
	});
});
