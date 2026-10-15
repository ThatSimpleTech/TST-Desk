import { expect, test, type Locator, type Page } from "@playwright/test";
import {
	controlNames,
	installShell,
	openPalette,
	readCensus,
	runCommand,
	runtime,
	writeCensus,
	type Runtime,
} from "./support";

/**
 * Walks the desktop window against a mock daemon.
 *
 * `e2e/census.json` is the list of controls each region showed. A new
 * button, field, or menu item fails this file until the census is
 * regenerated with `E2E_UPDATE_CENSUS=1` and the new control is given
 * an action in this spec (or an explicit reason it is display-only).
 */

const census: Record<string, string[]> = {};
const pageErrors: string[] = [];
let info: Runtime;

test.describe.configure({ mode: "serial" });

let page: Page;

test.beforeAll(async ({ browser }) => {
	info = runtime();
	page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
	page.on("pageerror", (err) => pageErrors.push(err.message));
	await installShell(page, info);
	await expect(page.getByRole("textbox", { name: "Filter sessions" })).toBeVisible();
	await expect(page.getByRole("dialog", { name: "First-run setup" })).toHaveCount(0);
});

test.afterAll(async () => {
	expect(pageErrors, pageErrors.join("\n")).toEqual([]);
	await page.close();
});

async function snap(region: string, root: Locator): Promise<void> {
	census[region] = await controlNames(root);
}

test("opens a workspace and gets a mock reply", async () => {
	await snap("chat", page.getByRole("region", { name: "Chat pane" }));
	await snap("tabs", page.getByRole("tablist", { name: "Right pane views" }));
	await runCommand(page, "New session");
	const composer = page.getByRole("textbox", { name: "Message composer" });
	await expect(composer).toBeEnabled();
	await composer.fill("Say hello");
	await page.getByRole("button", { name: "Send message" }).click();
	await expect(page.getByText(info.reply)).toBeVisible();
});

test("visits every right-pane view", async () => {
	for (const tab of ["Activity", "Files", "Work"]) {
		await page.getByRole("tab", { name: tab }).click();
		await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
	}
	const more = page
		.getByRole("tablist", { name: "Right pane views" })
		.locator("button[aria-haspopup='menu']");
	await more.click();
	const menu = page.getByRole("menu", { name: "More views" });
	await expect(menu).toBeVisible();
	await snap("more-views", menu);
	const items = ["Stack", "Usage", "Preview", "Plan"];
	if ((await page.getByRole("menuitemradio", { name: "Screen", exact: true }).count()) > 0) {
		items.splice(2, 0, "Screen");
	}
	for (const item of items) {
		if (!(await menu.isVisible())) await more.click();
		await page.getByRole("menuitemradio", { name: item, exact: true }).click();
		await expect(more).toContainText(item);
	}
});

test("visits every rail surface", async () => {
	const surfaces = page.getByRole("navigation", { name: "Surfaces" });
	await snap("surfaces", surfaces);
	for (const name of ["Projects", "Artifacts", "Scheduled"]) {
		await surfaces.getByRole("button", { name, exact: true }).click();
		await expect(
			surfaces.getByRole("button", { name: `${name} — you are here` }),
		).toBeVisible();
	}
	await snap("scheduled-list", page.getByRole("region", { name: "Scheduled" }));
	await snap("scheduled-form", page.getByRole("region", { name: "New scheduled job" }));
	await surfaces.getByRole("button", { name: "Home", exact: true }).click();
	await expect(surfaces.getByRole("button", { name: "Home — you are here" })).toBeVisible();
});

test("opens doctor and the decision ledger", async () => {
	await page.getByRole("button", { name: "Run doctor diagnostics" }).click();
	const doctor = page.getByRole("dialog", { name: "Doctor" });
	await expect(doctor.getByRole("button", { name: "Re-run" })).toBeEnabled();
	await snap("doctor", doctor);
	await page.keyboard.press("Escape");
	await expect(doctor).toHaveCount(0);

	await page.getByRole("button", { name: "Open decisions ledger" }).click();
	const decisions = page.getByRole("dialog", { name: "Decisions" });
	await expect(decisions).toBeVisible();
	await snap("decisions", decisions);
	await page.keyboard.press("Escape");
	await expect(decisions).toHaveCount(0);
});

test("walks every settings section and its enabled controls", async () => {
	test.setTimeout(120_000);
	await page.getByRole("button", { name: /Open settings/ }).click();
	const dialog = page.getByRole("dialog", { name: "Settings" });
	await expect(dialog).toBeVisible();
	const sections = [
		"Appearance",
		"Phones and devices",
		"Computers",
		"Computer use",
		"Engine",
		"Model",
		"Policy",
		"MCP servers",
		"API keys",
		"Email",
		"GitHub",
		"About",
	];
	for (const section of sections) {
		await dialog.getByRole("button", { name: section, exact: true }).click();
		await expect(dialog.getByRole("heading", { name: section })).toBeVisible();
		if (section === "GitHub") {
			await expect(dialog.getByText("GitHub is off until config.yaml")).toBeVisible();
		}
		const content = dialog.locator(".content");
		await snap(`settings:${section}`, content);
		await exercise(page, content);
		await expect(dialog).toBeVisible();
	}
	await page.keyboard.press("Escape");
	await expect(dialog).toHaveCount(0);
});

test("runs the command palette actions that stay in this window", async () => {
	await openPalette(page);
	const palette = page.getByRole("dialog", { name: "Command palette" });
	await snap("palette", palette);
	await page.keyboard.press("Escape");

	for (const title of [
		"Open decisions",
		"Run doctor",
		"Open stack",
		"Open work",
		"Open preview",
		"Open plan",
		"Open settings",
		"Toggle theme",
		"Stop computer use",
		"Resume computer use",
		"Toggle Design mode",
		"Check for updates",
		"New window",
		"Quit TST Desk",
	]) {
		await runCommand(page, title);
		await expect(page.getByRole("dialog", { name: "Command palette" })).toHaveCount(0);
		await page.keyboard.press("Escape");
	}
	await expect(page.getByRole("textbox", { name: "Filter sessions" })).toBeVisible();
	if (process.env.E2E_UPDATE_CENSUS === "1") writeCensus(census);
	else expect(census).toEqual(readCensus());
});

const SKIP_CLICK =
	/^(Close|Sign in with Microsoft|Sign in with Google|Connect GitHub|Disconnect|Send test email|Delete|Remove|Forget|Copy code|Open browser|Try again)$/;

async function exercise(page: Page, content: Locator): Promise<void> {
	const buttons = content.locator("button");
	const count = await buttons.count();
	for (let i = 0; i < count; i++) {
		const button = buttons.nth(i);
		if (!(await button.isVisible()) || !(await button.isEnabled())) continue;
		const name = ((await button.getAttribute("aria-label")) || (await button.innerText()))
			.replace(/\s+/g, " ")
			.trim();
		if (name === "" || SKIP_CLICK.test(name)) continue;
		await dismissExtraDialogs(page);
		await button.click({ timeout: 5_000 });
		await dismissExtraDialogs(page);
	}
}

/** Settings clicks can open a pane on top. Put Settings back in front. */
async function dismissExtraDialogs(page: Page): Promise<void> {
	for (const name of ["Computer use permissions", "Pair a phone"]) {
		const dialog = page.getByRole("dialog", { name });
		if (await dialog.isVisible()) {
			await dialog.getByRole("button", { name: "Close", exact: true }).click();
			await expect(dialog).toBeHidden();
		}
	}
	if ((await page.getByRole("dialog", { name: "Settings" }).count()) === 0) {
		await page.getByRole("button", { name: /Open settings/ }).click();
	}
}
