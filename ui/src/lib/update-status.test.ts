import { describe, expect, it } from "vitest";

import { actionLabel, bannerTitle, bannerVisible, primaryAction, type UpdateStatus } from "./update-status";

function status(partial: Partial<UpdateStatus>): UpdateStatus {
	return {
		phase: "idle",
		current: "0.1.0",
		latest: null,
		release_url: null,
		detail: "",
		automatic: true,
		can_apply: false,
		can_open_installer: false,
		announce: false,
		...partial,
	};
}

describe("update banner", () => {
	it("stays quiet while checking and when the user dismissed it", () => {
		expect(bannerVisible(status({ phase: "checking", announce: true }), false)).toBe(false);
		expect(bannerVisible(status({ phase: "up_to_date", announce: true }), false)).toBe(false);
		expect(
			bannerVisible(
				status({ phase: "ready", announce: true, latest: "0.2.0", can_apply: true }),
				true,
			),
		).toBe(false);
	});

	it("offers restart once the host has a verified package", () => {
		const ready = status({
			phase: "ready",
			announce: true,
			latest: "0.2.0",
			can_apply: true,
			detail: "Restart to install it.",
		});
		expect(bannerVisible(ready, false)).toBe(true);
		expect(bannerTitle(ready)).toBe("TST Desk 0.2.0 is ready");
		expect(primaryAction(ready)).toBe("restart");
		expect(actionLabel("restart")).toBe("Restart to update");
	});

	it("downloads only when the host says this install can take the file", () => {
		const manual = status({
			phase: "available",
			announce: true,
			latest: "0.2.0",
			can_apply: true,
			release_url: "https://github.com/ThatSimpleTech/TST-Desk/releases/tag/tstdesk-v0.2.0",
		});
		expect(primaryAction(manual)).toBe("download");
		const unpackaged = status({
			phase: "available",
			announce: true,
			latest: "0.2.0",
			release_url: manual.release_url,
		});
		expect(primaryAction(unpackaged)).toBe("releases");
	});

	it("opens an installer the host staged but will not apply itself", () => {
		const deb = status({
			phase: "ready",
			announce: true,
			latest: "0.2.0",
			can_open_installer: true,
		});
		expect(primaryAction(deb)).toBe("open");
	});
});
