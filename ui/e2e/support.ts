import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Locator, type Page } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));

export interface Runtime {
	port: number;
	token: string;
	workspace: string;
	reply: string;
	/** Messages the walk daemon seeded as held by a restart (TD-5158), and
	 *  the archived session holding them. */
	held: string[];
	held_session: string;
	/** The task list seeded in that session, in order (TD-5122). */
	tasks: string[];
	/** The done-check seeded on that session's reply (TD-5124). */
	check: string;
	/** The plan the mock model puts up when a turn plans (TD-5123). */
	plan: string;
}

export function runtime(): Runtime {
	const raw = readFileSync(path.join(here, ".runtime.json"), "utf8");
	return JSON.parse(raw) as Runtime;
}

const IDLE_UPDATE = {
	phase: "up_to_date",
	current: "0.2.1",
	latest: "0.2.1",
	release_url: null,
	detail: "",
	automatic: true,
	can_apply: false,
	can_open_installer: false,
	announce: false,
};

/** Pretend this page is the desktop shell and point it at the walk daemon. */
export async function installShell(page: Page, info: Runtime): Promise<string[]> {
	const invokes: string[] = [];
	await page.addInitScript(
		({ info, update }) => {
			const calls: string[] = [];
			(window as unknown as { __E2E_INVOKES: string[] }).__E2E_INVOKES = calls;
			const callbacks = new Map<number, (payload: unknown) => void>();
			let nextId = 1;
			(window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = {
				transformCallback(cb: (payload: unknown) => void) {
					const id = nextId++;
					callbacks.set(id, cb);
					return id;
				},
				unregisterCallback(id: number) {
					callbacks.delete(id);
				},
				convertFileSrc(filePath: string) {
					return filePath;
				},
				invoke(cmd: string, args?: { options?: { directory?: boolean } }) {
					calls.push(cmd);
					if (cmd === "get_daemon_info") {
						return { port: info.port, token: info.token, host: "127.0.0.1" };
					}
					if (cmd === "plugin:dialog|open" && args?.options?.directory) {
						return info.workspace;
					}
					if (
						cmd === "update_status" ||
						cmd === "check_for_updates" ||
						cmd === "download_update" ||
						cmd === "restart_to_update" ||
						cmd === "set_automatic_updates"
					) {
						return update;
					}
					if (cmd === "plugin:event|listen") return nextId++;
					return null;
				},
			};
		},
		{ info, update: IDLE_UPDATE },
	);
	await page.goto("/");
	page.on("pageerror", (err) => invokes.push(`pageerror:${err.message}`));
	return invokes;
}

export async function openPalette(page: Page): Promise<void> {
	await page.getByRole("button", { name: "Open command palette" }).click();
	await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
}

export async function runCommand(page: Page, title: string): Promise<void> {
	await openPalette(page);
	await page.getByRole("option", { name: title }).click();
}

/** Visible controls under root, as accessible names. Stable order. */
export async function controlNames(root: Locator): Promise<string[]> {
	const nodes = root.locator(
		"button, a, input, select, textarea, [role='tab'], [role='menuitem'], [role='menuitemradio'], [role='radio']",
	);
	const count = await nodes.count();
	const names: string[] = [];
	for (let i = 0; i < count; i++) {
		const node = nodes.nth(i);
		if (!(await node.isVisible())) continue;
		const aria = await node.getAttribute("aria-label");
		const text = (await node.innerText()).replace(/\s+/g, " ").trim();
		const placeholder = await node.getAttribute("placeholder");
		const raw = (aria || text || placeholder || "").replace(/\s+/g, " ").trim();
		const name = stableControlName(raw);
		if (name !== "") names.push(name);
	}
	return [...new Set(names)].sort();
}

/** Drop values that change between runs: session ids, toggle state, spend. */
function stableControlName(name: string): string {
	if (name.startsWith("Attach to ")) return "Attach to <session>";
	if (name === "On" || name === "Off") return "toggle";
	if (/^\$[\d.]+$/.test(name)) return "<spend>";
	return name.replace(/\b[0-9a-f]{8}\b/g, "<id>");
}

export function readCensus(): Record<string, string[]> {
	return JSON.parse(readFileSync(path.join(here, "census.json"), "utf8")) as Record<
		string,
		string[]
	>;
}

export function writeCensus(census: Record<string, string[]>): void {
	const ordered: Record<string, string[]> = {};
	for (const key of Object.keys(census).sort()) ordered[key] = census[key] ?? [];
	writeFileSync(path.join(here, "census.json"), `${JSON.stringify(ordered, null, "\t")}\n`, "utf8");
}
