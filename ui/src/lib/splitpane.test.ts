import { describe, it, expect } from "vitest";
import {
	MIN_RIGHT_PX,
	MIN_LEFT_PX,
	DEFAULT_RIGHT_PX,
	DIVIDER_HIT_MIN_PX,
	STORAGE_KEY,
	attachDragListeners,
	clampRightPx,
	maxRightPx,
	pointerToRightPx,
	readPersistedRightPx,
	writePersistedRightPx,
	type DragTarget,
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

describe("maxRightPx", () => {
	it("leaves the chat its minimum", () => {
		expect(maxRightPx(1000)).toBe(1000 - MIN_LEFT_PX);
	});

	it("does not drop below the inspector minimum on a narrow container", () => {
		expect(maxRightPx(MIN_LEFT_PX)).toBe(MIN_RIGHT_PX);
		expect(maxRightPx(0)).toBe(MIN_RIGHT_PX);
	});
});

describe("clampRightPx", () => {
	it("keeps an in-range width", () => {
		expect(clampRightPx(400, 1000)).toBe(400);
	});

	it("floors at the inspector minimum", () => {
		expect(clampRightPx(10, 1000)).toBe(MIN_RIGHT_PX);
	});

	it("caps at the container minus the chat minimum", () => {
		expect(clampRightPx(9999, 1000)).toBe(1000 - MIN_LEFT_PX);
	});

	it("has no container cap when width is unknown, so a wide preference survives", () => {
		expect(clampRightPx(900)).toBe(900);
		expect(clampRightPx(10)).toBe(MIN_RIGHT_PX);
	});

	it.each([Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
		"falls back to the default for non-finite input %f",
		(input) => {
			expect(clampRightPx(input, 1000)).toBe(DEFAULT_RIGHT_PX);
		}
	);
});

describe("pointerToRightPx", () => {
	it("measures from the container's right edge", () => {
		expect(pointerToRightPx(700, 0, 1000)).toBe(300);
		expect(pointerToRightPx(800, 100, 1000)).toBe(300);
	});

	it("clamps the converted width", () => {
		expect(pointerToRightPx(0, 0, 1000)).toBe(1000 - MIN_LEFT_PX);
		expect(pointerToRightPx(1000, 0, 1000)).toBe(MIN_RIGHT_PX);
	});

	it.each([0, -10])("returns null when the container has no measurable width (%f)", (w) => {
		expect(pointerToRightPx(400, 0, w)).toBeNull();
	});
});

describe("readPersistedRightPx", () => {
	it("returns the default when nothing was persisted", () => {
		expect(readPersistedRightPx(fakeStorage())).toBe(DEFAULT_RIGHT_PX);
	});

	it("restores a persisted value", () => {
		expect(readPersistedRightPx(fakeStorage({ [STORAGE_KEY]: "480" }))).toBe(480);
	});

	it("floors an undersized persisted value and keeps a wide one", () => {
		expect(readPersistedRightPx(fakeStorage({ [STORAGE_KEY]: "5" }))).toBe(MIN_RIGHT_PX);
		expect(readPersistedRightPx(fakeStorage({ [STORAGE_KEY]: "2000" }))).toBe(2000);
	});

	it.each(["abc", "", " "])("falls back to the default for corrupt value %j", (raw) => {
		expect(readPersistedRightPx(fakeStorage({ [STORAGE_KEY]: raw }))).toBe(DEFAULT_RIGHT_PX);
	});
});

describe("writePersistedRightPx", () => {
	it("round-trips a value through storage", () => {
		const storage = fakeStorage();
		writePersistedRightPx(storage, 412);
		expect(readPersistedRightPx(storage)).toBe(412);
	});

	it("persists the floored value", () => {
		const storage = fakeStorage();
		writePersistedRightPx(storage, 1);
		expect(storage.data.get(STORAGE_KEY)).toBe(String(MIN_RIGHT_PX));
	});
});

describe("divider hit target (TD-1011)", () => {
	it("is at least 8px so the painted 4px rule is not the only target", () => {
		expect(DIVIDER_HIT_MIN_PX).toBeGreaterThanOrEqual(8);
	});
});

describe("attachDragListeners (TD-1011)", () => {
	function fakeTarget(): DragTarget & {
		listeners: Map<string, Set<(e: PointerEvent) => void>>;
		dispatch(type: "pointermove" | "pointerup" | "pointercancel", e: PointerEvent): void;
	} {
		const listeners = new Map<string, Set<(e: PointerEvent) => void>>();
		return {
			listeners,
			addEventListener(type, listener) {
				const set = listeners.get(type) ?? new Set();
				set.add(listener);
				listeners.set(type, set);
			},
			removeEventListener(type, listener) {
				listeners.get(type)?.delete(listener);
			},
			dispatch(type, e) {
				for (const listener of listeners.get(type) ?? []) listener(e);
			},
		};
	}

	it("delivers move and up on the target, not on the divider", () => {
		const target = fakeTarget();
		const seen: string[] = [];
		const detach = attachDragListeners(
			target,
			() => seen.push("move"),
			() => seen.push("up"),
		);
		target.dispatch("pointermove", {} as PointerEvent);
		target.dispatch("pointerup", {} as PointerEvent);
		expect(seen).toEqual(["move", "up"]);
		detach();
		target.dispatch("pointermove", {} as PointerEvent);
		target.dispatch("pointerup", {} as PointerEvent);
		expect(seen).toEqual(["move", "up"]);
	});

	it("treats cancel as up so a lost capture still ends the drag", () => {
		const target = fakeTarget();
		const seen: string[] = [];
		attachDragListeners(
			target,
			() => seen.push("move"),
			() => seen.push("up"),
		);
		target.dispatch("pointercancel", {} as PointerEvent);
		expect(seen).toEqual(["up"]);
	});
});
