// @vitest-environment jsdom
//
// Settings → Appearance → Background (TD-5202): one radio per background
// with a preview, Classic chosen on a fresh device, a click or the arrow
// keys switch and save the choice for this device, and the Motion and
// Click ripples switches save theirs — standing down under Off, and
// explaining themselves when the OS asks for less motion. A preview moves
// only under the pointer or keyboard focus: a click's focus does not keep
// a second copy of the background running. That rule is the tile's whole
// rule: it does not also rest when the window loses focus. Strength is a
// second radio group, Subtle or Vivid, saved per device, that stands down
// for Off and Classic, which have no veil.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import SettingsBackground from "./components/SettingsBackground.svelte";
import { BACKGROUNDS } from "./backgrounds/catalog";
import { BACKGROUND_KEY, MOTION_KEY, RIPPLES_KEY, STRENGTH_KEY } from "./backgrounds/prefs";
import { background, loadBackground, setBackground } from "./backgrounds/store.svelte.js";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

/** The background tiles, not the Strength choices. */
function radios(): HTMLButtonElement[] {
	return [...target.querySelectorAll<HTMLButtonElement>(".tiles [role='radio']")];
}

function strengthGroup(): HTMLElement {
	const group = target.querySelector<HTMLElement>("[role='radiogroup'][aria-labelledby='background-strength-title']");
	if (!group) throw new Error("no Strength group");
	return group;
}

function strength(name: string): HTMLButtonElement {
	const found = [...strengthGroup().querySelectorAll<HTMLButtonElement>("[role='radio']")].find(
		(r) => r.textContent === name,
	);
	if (!found) throw new Error(`no strength ${name}`);
	return found;
}

/** The hint under the Strength row. */
function strengthHint(): string {
	return strengthGroup().closest(".row")?.nextElementSibling?.textContent?.trim() ?? "";
}

function radio(name: string): HTMLButtonElement {
	const found = radios().find((r) => r.getAttribute("aria-label") === name);
	if (!found) throw new Error(`no tile ${name}`);
	return found;
}

/** The Classic preview's orbit shows its resting pose (no turn written)
 *  unless its tile is live. */
function classicStill(): boolean {
	const field = radio("Classic").querySelector<HTMLElement>(".orbit .field");
	if (!field) throw new Error("no Classic preview");
	return field.style.transform === "";
}

/** Answer :focus-visible for a tile the way the browser would: true for
 *  keyboard focus, false for the focus a mouse click gives a button. */
function focusVisible(tile: HTMLButtonElement, visible: boolean): void {
	const matches = Element.prototype.matches;
	vi.spyOn(tile, "matches").mockImplementation(function (this: Element, selector: string) {
		return selector === ":focus-visible" ? visible : matches.call(this, selector);
	});
}

function toggle(name: string): HTMLButtonElement {
	const row = [...target.querySelectorAll(".row")].find((r) => r.querySelector(".title")?.textContent === name);
	const button = row?.querySelector<HTMLButtonElement>("[role='switch']");
	if (!button) throw new Error(`no switch ${name}`);
	return button;
}

beforeEach(() => {
	// jsdom has no 2D canvas; the previews just stay blank.
	vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
	localStorage.clear();
	loadBackground();
	background.reducedMotion = false;
	target = document.createElement("div");
	document.body.appendChild(target);
	component = mount(SettingsBackground, { target });
	flushSync();
});

afterEach(() => {
	if (component !== null) unmount(component);
	component = null;
	target.remove();
	vi.restoreAllMocks();
	localStorage.clear();
});

describe("the tiles", () => {
	it("are one labelled radio group with a tile and preview per background", () => {
		const group = target.querySelector(".tiles");
		expect(group?.getAttribute("role")).toBe("radiogroup");
		expect(group?.getAttribute("aria-labelledby")).toBe("background-title");
		expect(radios().map((r) => r.getAttribute("aria-label"))).toEqual(BACKGROUNDS.map((b) => b.label));
		for (const r of radios()) expect(r.querySelector(".preview")).not.toBeNull();
	});

	it("start on Classic, the only tile in the tab order", () => {
		expect(radio("Classic").getAttribute("aria-checked")).toBe("true");
		expect(radios().filter((r) => r.tabIndex === 0)).toEqual([radio("Classic")]);
		expect(target.querySelector(".hint")?.textContent).toContain("orbit halo");
	});

	it("switch and save the choice on a click", () => {
		radio("Galaxy").click();
		flushSync();
		expect(background.id).toBe("galaxy");
		expect(localStorage.getItem(BACKGROUND_KEY)).toBe("galaxy");
		expect(radio("Galaxy").getAttribute("aria-checked")).toBe("true");
		expect(radio("Classic").getAttribute("aria-checked")).toBe("false");
		expect(target.querySelector(".hint")?.textContent).toContain("spiral galaxy");
	});

	it("move the choice with the arrow keys, wrapping at the ends", () => {
		const press = (key: string) => {
			const current = radios().find((r) => r.getAttribute("aria-checked") === "true");
			current?.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
			flushSync();
		};
		press("ArrowRight");
		expect(background.id).toBe("galaxy");
		press("ArrowLeft");
		press("ArrowLeft");
		expect(background.id).toBe("off");
		press("ArrowLeft");
		expect(background.id).toBe(BACKGROUNDS[BACKGROUNDS.length - 1].id);
		press("Home");
		expect(background.id).toBe("off");
		press("End");
		expect(document.activeElement).toBe(radios()[BACKGROUNDS.length - 1]);
	});
});

describe("the previews", () => {
	it("come alive under the pointer and hold still when it leaves", () => {
		expect(classicStill()).toBe(true);
		radio("Classic").dispatchEvent(new Event("pointerenter"));
		flushSync();
		expect(classicStill()).toBe(false);
		radio("Classic").dispatchEvent(new Event("pointerleave"));
		flushSync();
		expect(classicStill()).toBe(true);
	});

	it("come alive under keyboard focus, until it moves on", () => {
		const tile = radio("Classic");
		focusVisible(tile, true);
		tile.focus();
		flushSync();
		expect(classicStill()).toBe(false);
		tile.blur();
		flushSync();
		expect(classicStill()).toBe(true);
	});

	it("hold still after a click once the pointer leaves, though the tile keeps focus", () => {
		const tile = radio("Classic");
		focusVisible(tile, false);
		tile.dispatchEvent(new Event("pointerenter"));
		tile.focus();
		tile.click();
		tile.dispatchEvent(new Event("pointerleave"));
		flushSync();
		expect(document.activeElement).toBe(tile);
		expect(classicStill()).toBe(true);
	});

	it("keep moving under the pointer when the window loses focus: the tile's rule is the whole rule", () => {
		const queue = new Map<number, FrameRequestCallback>();
		let next = 1;
		vi.spyOn(window, "requestAnimationFrame").mockImplementation((run) => {
			queue.set(next, run);
			return next++;
		});
		vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
			queue.delete(id);
		});
		const focused = vi.spyOn(document, "hasFocus").mockReturnValue(true);
		radio("Classic").dispatchEvent(new Event("pointerenter"));
		flushSync();
		expect(queue.size).toBe(1);
		focused.mockReturnValue(false);
		window.dispatchEvent(new Event("blur"));
		flushSync();
		expect(queue.size).toBe(1);
		const field = radio("Classic").querySelector<HTMLElement>(".orbit .field");
		const before = field?.style.transform;
		let now = 0;
		for (let i = 0; i < 12; i++) {
			now += 1000 / 60;
			const runs = [...queue.values()];
			queue.clear();
			for (const run of runs) run(now);
		}
		flushSync();
		expect(field?.style.transform).not.toBe(before);
	});

	it("bind their tiles without a dev-mode warning", () => {
		if (component !== null) unmount(component);
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		component = mount(SettingsBackground, { target });
		flushSync();
		const binding = warn.mock.calls.filter((call) => String(call[0]).includes("binding_property_non_reactive"));
		expect(binding).toEqual([]);
	});
});

describe("the switches", () => {
	it("are named by their row titles, with On and Off as the state", () => {
		for (const name of ["Motion", "Click ripples"]) {
			const id = toggle(name).getAttribute("aria-labelledby");
			expect(id).not.toBeNull();
			expect(document.getElementById(id ?? "")?.textContent).toBe(name);
		}
	});

	it("save Motion and Click ripples for this device", () => {
		toggle("Motion").click();
		toggle("Click ripples").click();
		flushSync();
		expect(background.motion).toBe(false);
		expect(background.ripples).toBe(false);
		expect(localStorage.getItem(MOTION_KEY)).toBe("off");
		expect(localStorage.getItem(RIPPLES_KEY)).toBe("off");
		expect(toggle("Motion").textContent).toBe("Off");
		expect(toggle("Motion").getAttribute("aria-checked")).toBe("false");
	});

	it("stand down under Off, which has nothing to move", () => {
		setBackground("off");
		flushSync();
		expect(toggle("Motion").disabled).toBe(true);
		expect(toggle("Click ripples").disabled).toBe(true);
		expect(target.textContent).toContain("Off has nothing to move");
	});

	it("say why nothing moves when the OS asks for less motion", () => {
		background.reducedMotion = true;
		flushSync();
		expect(target.textContent).toContain("Your system asks for less motion");
		expect(toggle("Motion").disabled).toBe(false);
	});
});

describe("Strength", () => {
	it("is a radio group named by its row, Subtle and Vivid, starting on Subtle", () => {
		expect(document.getElementById("background-strength-title")?.textContent).toBe("Strength");
		const options = [...strengthGroup().querySelectorAll<HTMLButtonElement>("[role='radio']")];
		expect(options.map((o) => o.textContent)).toEqual(["Subtle", "Vivid"]);
		expect(strength("Subtle").getAttribute("aria-checked")).toBe("true");
		expect(options.filter((o) => o.tabIndex === 0)).toEqual([strength("Subtle")]);
	});

	it("stands down for Classic, which has no veil, and says so", () => {
		expect(background.id).toBe("classic");
		expect(strength("Subtle").disabled).toBe(true);
		expect(strength("Vivid").disabled).toBe(true);
		expect(strengthHint()).toContain("Classic");
		setBackground("off");
		flushSync();
		expect(strength("Vivid").disabled).toBe(true);
		expect(strengthHint()).toContain("Off draws nothing");
	});

	it("switches and saves on a click, for a background behind a veil", () => {
		setBackground("galaxy");
		flushSync();
		expect(strength("Vivid").disabled).toBe(false);
		expect(strengthHint()).toContain("all text stays clear");
		strength("Vivid").click();
		flushSync();
		expect(background.strength).toBe("vivid");
		expect(localStorage.getItem(STRENGTH_KEY)).toBe("vivid");
		expect(strength("Vivid").getAttribute("aria-checked")).toBe("true");
		expect(strength("Subtle").getAttribute("aria-checked")).toBe("false");
		expect(strength("Vivid").tabIndex).toBe(0);
		expect(strengthHint()).toContain("Small grey text can be harder to read");
	});

	it("moves with the arrow keys, wrapping, and keeps the focus on the choice", () => {
		setBackground("aurora");
		flushSync();
		const press = (key: string) => {
			const current = [...strengthGroup().querySelectorAll<HTMLButtonElement>("[role='radio']")].find(
				(r) => r.getAttribute("aria-checked") === "true",
			);
			current?.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
			flushSync();
		};
		press("ArrowRight");
		expect(background.strength).toBe("vivid");
		expect(document.activeElement).toBe(strength("Vivid"));
		press("ArrowRight");
		expect(background.strength).toBe("subtle");
		press("End");
		expect(background.strength).toBe("vivid");
		press("Home");
		expect(background.strength).toBe("subtle");
		expect(localStorage.getItem(STRENGTH_KEY)).toBe("subtle");
		// The tiles' keys are their own: the background did not move.
		expect(background.id).toBe("aurora");
	});
});
