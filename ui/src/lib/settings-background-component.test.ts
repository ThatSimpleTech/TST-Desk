// @vitest-environment jsdom
//
// Settings → Appearance → Background (TD-5202): one radio per background
// with a preview, Classic chosen on a fresh device, a click or the arrow
// keys switch and save the choice for this device, and the Motion and
// Click ripples switches save theirs — standing down under Off, and
// explaining themselves when the OS asks for less motion.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import SettingsBackground from "./components/SettingsBackground.svelte";
import { BACKGROUNDS } from "./backgrounds/catalog";
import { BACKGROUND_KEY, MOTION_KEY, RIPPLES_KEY } from "./backgrounds/prefs";
import { background, loadBackground, setBackground } from "./backgrounds/store.svelte.js";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

function radios(): HTMLButtonElement[] {
	return [...target.querySelectorAll<HTMLButtonElement>("[role='radio']")];
}

function radio(name: string): HTMLButtonElement {
	const found = radios().find((r) => r.getAttribute("aria-label") === name);
	if (!found) throw new Error(`no tile ${name}`);
	return found;
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
		const group = target.querySelector("[role='radiogroup']");
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

describe("the switches", () => {
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
