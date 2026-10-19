import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
	testDir: "./e2e",
	testMatch: /.*\.spec\.ts/,
	timeout: 60_000,
	expect: { timeout: 15_000 },
	fullyParallel: false,
	workers: 1,
	retries: 0,
	reporter: "line",
	globalSetup: "./e2e/global-setup.ts",
	use: {
		baseURL: "http://127.0.0.1:5173",
		viewport: { width: 1280, height: 800 },
		trace: "retain-on-failure",
	},
	projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
	webServer: {
		command: "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort",
		url: "http://127.0.0.1:5173",
		reuseExistingServer: false,
		timeout: 120_000,
	},
});
