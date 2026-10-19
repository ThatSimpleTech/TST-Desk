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
	// Clipboard access so "Copy message as markdown" can be read back.
	page = await browser.newPage({
		viewport: { width: 1280, height: 800 },
		permissions: ["clipboard-read", "clipboard-write"],
	});
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

	// With a session open the composer grows the model bar and the
	// attach menu.
	await snap("chat:session", page.getByRole("region", { name: "Chat pane" }));
	const attach = page.getByRole("button", { name: "Attach a file or take a photo" });
	await attach.click();
	const attachMenu = page.getByRole("menu", { name: "Add attachment" });
	await expect(attachMenu).toBeVisible();
	await snap("attach-menu", attachMenu);
	// Upload opens the OS picker and Take a photo opens the camera; both
	// leave this window, so the walk only checks they are offered.
	await attach.click();
	await expect(attachMenu).toHaveCount(0);

	await walkModelBar();
	await walkMessageActions();
});

/**
 * Plan, Model tier and Session preset each go to the daemon, and the bar
 * only moves when its tier_state comes back. A select keeps whatever was
 * picked even if nothing is sent, so each step checks something only the
 * daemon's answer can change: the pin hint, aria-pressed, the disabled
 * tiers, the model names in the tier labels.
 */
async function walkModelBar(): Promise<void> {
	const bar = page.getByRole("group", { name: "Model" });
	const tier = bar.getByRole("combobox", { name: "Model tier" });
	const preset = bar.getByRole("combobox", { name: "Session preset" });
	const plan = bar.getByRole("button", { name: "Plan", exact: true });

	await expect(tier).toHaveAttribute("title", /^Choosing a tier pins it/);
	await tier.selectOption("worker");
	await expect(tier).toHaveAttribute("title", /^Pinned for this session/);
	await expect(tier).toHaveValue("worker");

	await expect(plan).toHaveAttribute("aria-pressed", "false");
	await plan.click();
	await expect(plan).toHaveAttribute("aria-pressed", "true");
	await expect(tier).toHaveValue("brain");
	for (const locked of ["worker", "validator"]) {
		await expect(tier.locator(`option[value='${locked}']`)).toBeDisabled();
	}
	await plan.click();
	await expect(plan).toHaveAttribute("aria-pressed", "false");
	await expect(tier.locator("option[value='worker']")).toBeEnabled();

	const start = await preset.inputValue();
	const other = (await preset.locator("option").allInnerTexts()).find((p) => p !== start);
	if (other === undefined) throw new Error("the walk needs a second preset to switch to");
	const brain = tier.locator("option[value='brain']");
	const startLabel = await brain.innerText();
	await preset.selectOption(other);
	await expect(brain).not.toHaveText(startLabel);
	await expect(preset).toHaveValue(other);
	await preset.selectOption(start);
	await expect(brain).toHaveText(startLabel);
}

/** Copy, Retry, Edit and Summarize from here on the first exchange; Retry and Edit fork it. */
async function walkMessageActions(): Promise<void> {
	const chat = page.getByRole("region", { name: "Chat pane" });

	const copy = chat.getByRole("button", { name: "Copy message as markdown" });
	await copy.click();
	await expect(copy).toHaveAttribute("title", "Copied");
	expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(info.reply);

	// One Edit button per user row: a fork must replace the row, not add one.
	const edit = chat.getByRole("button", { name: "Edit message" });
	await chat.getByRole("button", { name: "Retry: resend your last message" }).click();
	await expect(chat.getByText("2/2", { exact: true })).toBeVisible();
	await expect(chat.getByText(info.reply)).toBeVisible();
	await expect(edit).toHaveCount(1);

	await edit.click();
	await chat.locator("textarea.edit").fill("Say hello again");
	await chat.getByRole("button", { name: "Save", exact: true }).click();
	await expect(chat.getByText("3/3", { exact: true })).toBeVisible();
	await expect(chat.getByText("Say hello again")).toBeVisible();
	await expect(chat.getByText(info.reply)).toBeVisible();
	await expect(edit).toHaveCount(1);

	await chat.getByRole("button", { name: "Previous branch" }).click();
	await expect(chat.getByText("2/3", { exact: true })).toBeVisible();
	await expect(chat.getByText("Say hello", { exact: true })).toBeVisible();
	await chat.getByRole("button", { name: "Next branch" }).click();
	await expect(chat.getByText("3/3", { exact: true })).toBeVisible();

	// TD-5109: the model's copy becomes a summary; this window keeps every row.
	await chat.getByRole("button", { name: "Summarize from here" }).first().click();
	await expect(page.getByText("Summarized from a message").first()).toBeAttached();
	await expect(chat.getByText("Say hello again")).toBeVisible();
}

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
	await snap("settings-nav", dialog.getByRole("navigation", { name: "Settings sections" }));
	const sections = [
		"Appearance",
		"Phones and devices",
		"Computers",
		"Computer use",
		"Engine",
		"Model",
		"Budget",
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
		if (section === "Budget") {
			// Saving a number is what puts "of $…" on the title bar.
			await dialog.getByRole("textbox", { name: "Spend" }).fill("5");
			await dialog.getByRole("button", { name: "Save", exact: true }).click();
			await expect(dialog.getByRole("button", { name: "Clear", exact: true })).toBeVisible();
			await expect(page.getByText(/^of \$5/)).toBeVisible();
		}
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
			// "Close" after an error; "Cancel" while a pairing code is showing
			// (a machine on a tailnet gets a real code).
			await dialog.getByRole("button", { name: /^(Close|Cancel)$/ }).click();
			await expect(dialog).toBeHidden();
		}
	}
	if ((await page.getByRole("dialog", { name: "Settings" }).count()) === 0) {
		await page.getByRole("button", { name: /Open settings/ }).click();
	}
}
