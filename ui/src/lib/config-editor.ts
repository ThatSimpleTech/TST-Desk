// Settings → Configuration (TD-5203): pure helpers over the daemon's schema.
//
// The daemon sends every config model as a def and every value as a tree;
// these map one node to its control, read values and defaults at a path,
// turn what a person typed into a value (or a message), and decide which
// keys a search shows. No runes and no I/O, so each is unit-tested alone.

import type {
	ConfigModelDef,
	ConfigNode,
	ConfigPathPart,
	JsonValue,
} from "./protocol";
import type { SettingsSection } from "./settings.svelte.js";

export type ConfigPath = ConfigPathPart[];

/** The key `defaults` and the store use for a path (the daemon's compact JSON). */
export function pathKey(path: ConfigPath): string {
	return JSON.stringify(path);
}

/** `presets.local.brain.fallbacks[0]`: how the docs name a key. */
export function dotted(path: ConfigPath): string {
	return path
		.map((part, i) => (typeof part === "number" ? `[${part}]` : i ? `.${part}` : part))
		.join("");
}

export function getAt(root: JsonValue | undefined, path: ConfigPath): JsonValue | undefined {
	let node: JsonValue | undefined = root;
	for (const part of path) {
		if (Array.isArray(node) && typeof part === "number") node = node[part];
		else if (node !== null && typeof node === "object" && !Array.isArray(node) && typeof part === "string")
			node = node[part];
		else return undefined;
	}
	return node;
}

export function deepEqual(a: JsonValue | undefined, b: JsonValue | undefined): boolean {
	if (a === b) return true;
	if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
	if (Array.isArray(a) !== Array.isArray(b)) return false;
	if (Array.isArray(a) && Array.isArray(b))
		return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
	const left = a as Record<string, JsonValue>;
	const right = b as Record<string, JsonValue>;
	const keys = Object.keys(left);
	return keys.length === Object.keys(right).length && keys.every((k) => deepEqual(left[k], right[k]));
}

export type Control =
	| "toggle"
	| "choice"
	| "select"
	| "number"
	| "text"
	| "textarea"
	| "list"
	| "chips"
	| "json"
	| "union"
	| "secrets"
	| "group"
	| "entries";

/** A segmented control fits a few short choices; more get a select. */
const SEGMENT_MAX = 4;
const SEGMENT_LABEL_MAX = 12;

export function controlFor(node: ConfigNode): Control {
	switch (node.kind) {
		case "secrets":
			return "secrets";
		case "bool":
			return "toggle";
		case "enum": {
			const short = node.choices.every((c) => String(c).length <= SEGMENT_LABEL_MAX);
			return node.choices.length <= SEGMENT_MAX && short ? "choice" : "select";
		}
		case "int":
		case "float":
			return "number";
		case "str":
			if (node.choices_from.length > 0) return "select";
			return node.multiline ? "textarea" : "text";
		case "list":
			if (node.items?.kind === "object") return "entries";
			return node.items?.kind === "enum" ? "chips" : "list";
		case "map":
			return node.entries?.kind === "object" ? "entries" : "json";
		case "object":
			return "group";
		case "union":
			return "union";
		default:
			return "json";
	}
}

/** Groups and entry lists hold keys; everything else is one value with one control. */
export function isLeaf(node: ConfigNode): boolean {
	const control = controlFor(node);
	return control !== "group" && control !== "entries";
}

export interface DefaultValue {
	has: boolean;
	value: JsonValue;
}

/**
 * What Reset gives. A key at a fixed place has its reset value in the
 * daemon's `defaults` (what a fresh install loads); a key inside a list
 * item or map entry takes its model's built-in default.
 */
export function defaultFor(
	defaults: Record<string, JsonValue>,
	path: ConfigPath,
	node: ConfigNode,
): DefaultValue {
	// A key its own control sets (the active model) has nothing to go back to.
	if (!node.resettable) return { has: false, value: null };
	const key = pathKey(path);
	if (key in defaults) return { has: true, value: defaults[key] };
	return { has: node.has_default, value: node.has_default ? node.default : null };
}

export function isModified(value: JsonValue | undefined, fallback: DefaultValue): boolean {
	return fallback.has && !deepEqual(value ?? null, fallback.value);
}

/** The values a select offers: the literal's, or the keys of the map the node names. */
export function choicesFor(node: ConfigNode, values: Record<string, JsonValue>): JsonValue[] {
	if (node.choices_from.length === 0) return node.choices;
	const source = getAt(values, node.choices_from);
	return source !== null && typeof source === "object" && !Array.isArray(source)
		? Object.keys(source)
		: [];
}

export type Parsed = { ok: true; value: JsonValue } | { ok: false; error: string };

/** The bounds of a number, in words for the hint under its field. */
export function boundsHint(node: ConfigNode): string {
	const low =
		node.minimum !== null ? `at least ${node.minimum}` : node.exclusive_minimum !== null ? `more than ${node.exclusive_minimum}` : "";
	const high =
		node.maximum !== null ? `at most ${node.maximum}` : node.exclusive_maximum !== null ? `less than ${node.exclusive_maximum}` : "";
	if (node.minimum !== null && node.maximum !== null) return `${node.minimum} to ${node.maximum}`;
	return [low, high].filter(Boolean).join(", ");
}

export function parseNumber(text: string, node: ConfigNode): Parsed {
	const trimmed = text.trim();
	if (trimmed === "") return node.nullable ? { ok: true, value: null } : { ok: false, error: "Enter a number." };
	const value = Number(trimmed);
	if (!Number.isFinite(value)) return { ok: false, error: "That is not a number." };
	if (node.kind === "int" && !Number.isInteger(value)) return { ok: false, error: "Use a whole number." };
	if (node.minimum !== null && value < node.minimum) return { ok: false, error: `Use at least ${node.minimum}.` };
	if (node.maximum !== null && value > node.maximum) return { ok: false, error: `Use at most ${node.maximum}.` };
	if (node.exclusive_minimum !== null && value <= node.exclusive_minimum)
		return { ok: false, error: `Use more than ${node.exclusive_minimum}.` };
	if (node.exclusive_maximum !== null && value >= node.exclusive_maximum)
		return { ok: false, error: `Use less than ${node.exclusive_maximum}.` };
	return { ok: true, value };
}

export function parseText(text: string, node: ConfigNode): Parsed {
	if (node.min_length !== null && text.length < node.min_length)
		return { ok: false, error: node.min_length === 1 ? "This cannot be empty." : `Use at least ${node.min_length} characters.` };
	if (node.max_length !== null && text.length > node.max_length)
		return { ok: false, error: `Use at most ${node.max_length} characters.` };
	return { ok: true, value: text };
}

export function parseJsonText(text: string, node: ConfigNode): Parsed {
	if (text.trim() === "") return node.nullable ? { ok: true, value: null } : { ok: true, value: {} };
	try {
		return { ok: true, value: JSON.parse(text) as JsonValue };
	} catch {
		return { ok: false, error: "That is not valid JSON." };
	}
}

/** JSON as the box shows it: two-space indent, empty for not set. */
export function jsonText(value: JsonValue | undefined): string {
	return value === null || value === undefined ? "" : JSON.stringify(value, null, 2);
}

/**
 * Words the way a shell splits them, for a command typed on one line:
 * spaces separate, and "double" or 'single' quotes keep spaces in a word.
 */
export function splitWords(text: string): string[] {
	const words: string[] = [];
	let word = "";
	let quote: string | null = null;
	let started = false;
	for (const ch of text) {
		if (quote !== null) {
			if (ch === quote) quote = null;
			else word += ch;
		} else if (ch === '"' || ch === "'") {
			quote = ch;
			started = true;
		} else if (/\s/.test(ch)) {
			if (started) words.push(word);
			word = "";
			started = false;
			continue;
		} else {
			word += ch;
		}
		started = true;
	}
	if (started) words.push(word);
	return words;
}

/** One line for a list of words, quoting a word that holds a space. */
export function joinWords(words: JsonValue[]): string {
	return words.map((w) => (/\s/.test(String(w)) ? `"${String(w)}"` : String(w))).join(" ");
}

const BYTE_UNITS = ["KiB", "MiB", "GiB", "TiB"];

/** 536870912 → "512 MiB": a byte count a person can read. */
export function readableBytes(bytes: number): string {
	let size = bytes;
	let unit = "bytes";
	for (const next of BYTE_UNITS) {
		if (size < 1024) break;
		size /= 1024;
		unit = next;
	}
	return `${Number.isInteger(size) ? size : size.toFixed(1)} ${unit}`;
}

/** A value as the hint under a field names it; a byte count also in KiB, MiB or GiB. */
export function formatValue(value: JsonValue | undefined, unit = ""): string {
	if (value === null || value === undefined) return "not set";
	if (value === true) return "on";
	if (value === false) return "off";
	if (value === "") return "empty";
	if (Array.isArray(value)) return value.length === 0 ? "none" : value.map((v) => formatValue(v)).join(", ");
	if (typeof value === "object") return Object.keys(value).length === 0 ? "none" : JSON.stringify(value);
	if (unit === "bytes" && typeof value === "number" && value >= 1024) return `${value} (${readableBytes(value)})`;
	return String(value);
}

/** A list item's or map entry's name on its card. */
export function entryLabel(value: JsonValue | undefined, fallback: string): string {
	if (value !== null && typeof value === "object" && !Array.isArray(value)) {
		for (const key of ["name", "label", "slug", "language_id"]) {
			const v = value[key];
			if (typeof v === "string" && v.trim()) return v;
		}
		const command = value.command;
		if (Array.isArray(command) && command.length > 0) return joinWords(command);
		if (typeof command === "string" && command.trim()) return command;
	}
	return fallback;
}

/** The value a new entry is written with: the add form's filled fields only. */
export function newEntry(def: ConfigModelDef, drafts: Record<string, JsonValue>): Record<string, JsonValue> {
	const entry: Record<string, JsonValue> = {};
	for (const name of def.add_fields) {
		const value = drafts[name];
		const required = def.fields.find((f) => f.name === name)?.required ?? false;
		const empty = value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0);
		if (!empty || required) entry[name] = value ?? null;
	}
	return entry;
}

function haystack(path: ConfigPath, node: ConfigNode): string {
	return [dotted(path), node.title, node.description, node.unit].join(" ").toLowerCase();
}

/**
 * The path keys a search shows, or null when there is no search. A key
 * matches when every word of the query is in its name, title or words; its
 * parents show so it can be reached, and a matching group shows all of it.
 */
export function visibleKeys(
	query: string,
	root: string,
	defs: Record<string, ConfigModelDef>,
	values: Record<string, JsonValue>,
): Set<string> | null {
	const words = query.toLowerCase().split(/\s+/).filter(Boolean);
	if (words.length === 0) return null;
	const shown = new Set<string>();
	const reveal = (path: ConfigPath) => {
		for (let i = 1; i <= path.length; i++) shown.add(pathKey(path.slice(0, i)));
	};
	const walk = (node: ConfigNode, path: ConfigPath, within: boolean, depth: number) => {
		const hit = within || words.every((w) => haystack(path, node).includes(w));
		if (hit) reveal(path);
		if (depth > 8) return;
		for (const [childPath, child] of children(node, path, defs, values)) walk(child, childPath, hit, depth + 1);
	};
	for (const field of defs[root]?.fields ?? []) walk(field.node, [field.name], false, 0);
	return shown;
}

/** The keys under a node that exist now: a group's fields, an entry list's entries. */
export function children(
	node: ConfigNode,
	path: ConfigPath,
	defs: Record<string, ConfigModelDef>,
	values: Record<string, JsonValue>,
): [ConfigPath, ConfigNode][] {
	if (node.kind === "object" && node.ref !== null) {
		if (node.nullable && getAt(values, path) === null) return [];
		return (defs[node.ref]?.fields ?? []).map((f) => [[...path, f.name], f.node]);
	}
	const held = getAt(values, path);
	if (node.kind === "list" && node.items?.kind === "object" && Array.isArray(held))
		return held.map((_, i) => [[...path, i], node.items as ConfigNode]);
	if (node.kind === "map" && node.entries?.kind === "object" && held && typeof held === "object" && !Array.isArray(held))
		return Object.keys(held).map((k) => [[...path, k], node.entries as ConfigNode]);
	return [];
}

/**
 * The Settings pages that also edit some keys of a section, for its
 * "Also in" links. Which page shows what is the window's own knowledge;
 * what may be written here is the daemon's (`editable`, `note`).
 */
export const SECTION_PAGES: Record<string, SettingsSection[]> = {
	presets: ["model"],
	active_preset: ["model"],
	model_refresh: ["model"],
	credentials: ["key"],
	mcp: ["mcp"],
	engine: ["engine"],
	judgments: ["computer"],
	notify: ["email"],
	connectors: ["github", "email"],
	remote: ["devices"],
	speech: ["appearance"],
	voice: ["appearance"],
};

/** Those pages' names, as the Settings nav shows them. */
export const PAGE_LABEL: Partial<Record<SettingsSection, string>> = {
	model: "Model",
	key: "API keys",
	mcp: "MCP servers",
	engine: "Engine",
	computer: "Computer use",
	email: "Email",
	github: "GitHub",
	devices: "Phones and devices",
	appearance: "Appearance",
};

/** How many keys at a fixed place in a section differ from a fresh install's. */
export function changedCount(
	section: string,
	defaults: Record<string, JsonValue>,
	values: Record<string, JsonValue>,
): number {
	let count = 0;
	for (const [key, value] of Object.entries(defaults)) {
		const path = JSON.parse(key) as ConfigPath;
		if (path[0] !== section) continue;
		if (!deepEqual(getAt(values, path) ?? null, value)) count++;
	}
	return count;
}
