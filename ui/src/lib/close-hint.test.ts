import { describe, it, expect, beforeEach } from "vitest";
import { banners, clearNotifications } from "./notifications.svelte.js";
import {
	CLOSE_IS_NOT_QUIT,
	showCloseIsNotQuitNotice,
} from "./close-hint";

beforeEach(() => {
	clearNotifications();
});

describe("close is not quit", () => {
	it("names Quit TST Desk and says close hides", () => {
		expect(CLOSE_IS_NOT_QUIT.body).toContain("Quit TST Desk");
		expect(CLOSE_IS_NOT_QUIT.body).toContain("hides");
	});

	it("raises one banner and does not stack a second", () => {
		showCloseIsNotQuitNotice();
		showCloseIsNotQuitNotice();
		expect(banners).toHaveLength(1);
		expect(banners[0]?.key).toBe("close-is-not-quit");
		expect(banners[0]?.title).toBe(CLOSE_IS_NOT_QUIT.title);
		expect(banners[0]?.body).toBe(CLOSE_IS_NOT_QUIT.body);
	});
});
