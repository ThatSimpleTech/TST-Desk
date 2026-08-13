import { describe, it, expect } from "vitest";
import {
	MIN_LEFT_PCT,
	MAX_LEFT_PCT,
	DEFAULT_LEFT_PCT,
	STORAGE_KEY,
	clampLeftPct,
	pxToLeftPct,
	readPersistedLeftPct,
	writePersistedLeftPct,
	type KVStorage
} from "./splitpane";

function fakeStorage(initial: Record<string, string> = {}): KVStorage & { data: Map<string, string> } {
	const data = new Map<string, string>(Object.entries(initial));
	return {
		data,
		getItem: (key) => data.get(key) ?? null,
		setItem: (key, value) => void data.set(key, value)
	};
}

describe("clampLeftPct", () => {
	it.each([
		[0, MIN_LEFT_PCT],
		[19.9, MIN_LEFT_PCT],
		[MIN_LEFT_PCT, MIN_LEFT_PCT],
		[50, 50],
		[MAX_LEFT_PCT, MAX_LEFT_PCT],
		[80.1, MAX_LEFT_PCT],
		[100, MAX_LEFT_PCT]
	])("clamps %f to %f", (input, expected) => {
		expect(clampLeftPct(input)).toBe(expected);
	});

	it.each([Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
		"falls back to the default for non-finite input %f",
		(input) => {
			expect(clampLeftPct(input)).toBe(DEFAULT_LEFT_PCT);
		}
	);
});

describe("pxToLeftPct", () => {
	it("converts a pixel offset to a percentage of container width", () => {
		expect(pxToLeftPct(500, 1000)).toBe(50);
	});

	it("clamps the converted percentage", () => {
		expect(pxToLeftPct(50, 1000)).toBe(MIN_LEFT_PCT);
		expect(pxToLeftPct(950, 1000)).toBe(MAX_LEFT_PCT);
	});

	it.each([0, -10])("returns null when the container has no measurable width (%f)", (w) => {
		expect(pxToLeftPct(400, w)).toBeNull();
	});
});

describe("readPersistedLeftPct", () => {
	it("returns the default when nothing was persisted", () => {
		expect(readPersistedLeftPct(fakeStorage())).toBe(DEFAULT_LEFT_PCT);
	});

	it("restores a persisted value", () => {
		expect(readPersistedLeftPct(fakeStorage({ [STORAGE_KEY]: "55" }))).toBe(55);
	});

	it("clamps an out-of-range persisted value", () => {
		expect(readPersistedLeftPct(fakeStorage({ [STORAGE_KEY]: "95" }))).toBe(MAX_LEFT_PCT);
		expect(readPersistedLeftPct(fakeStorage({ [STORAGE_KEY]: "5" }))).toBe(MIN_LEFT_PCT);
	});

	it.each(["abc", "", " "])("falls back to the default for corrupt value %j", (raw) => {
		expect(readPersistedLeftPct(fakeStorage({ [STORAGE_KEY]: raw }))).toBe(DEFAULT_LEFT_PCT);
	});
});

describe("writePersistedLeftPct", () => {
	it("round-trips a value through storage", () => {
		const storage = fakeStorage();
		writePersistedLeftPct(storage, 33.7);
		expect(readPersistedLeftPct(storage)).toBe(33.7);
	});

	it("persists the clamped value", () => {
		const storage = fakeStorage();
		writePersistedLeftPct(storage, 200);
		expect(storage.data.get(STORAGE_KEY)).toBe(String(MAX_LEFT_PCT));
	});
});
