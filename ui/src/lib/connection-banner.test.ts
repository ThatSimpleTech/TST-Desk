import { describe, expect, it } from "vitest";
import { bannerLabel, bannerOffersPairAgain, bannerTone } from "./connection-banner";

describe("bannerLabel (TD-1304)", () => {
	it("says Connected only when the socket has handshaken", () => {
		expect(bannerLabel("connected", "connected")).toBe("Connected");
		expect(bannerTone("connected", "connected")).toBe("success");
	});

	it("does not stay on Connecting… after the host gives up", () => {
		expect(bannerLabel("connecting", "stopped")).toBe("Couldn’t start the daemon");
		expect(bannerLabel("reconnecting", "stopped")).toBe("Couldn’t start the daemon");
		expect(bannerLabel("disconnected", "stopped")).toBe("Couldn’t start the daemon");
		expect(bannerTone("connecting", "stopped")).toBe("warning");
	});

	it("still says Connecting… while the host is starting or retrying", () => {
		expect(bannerLabel("connecting", "starting")).toBe("Connecting…");
		expect(bannerLabel("connecting", "crashed")).toBe("Connecting…");
		expect(bannerTone("connecting", "starting")).toBe("info");
	});

	it("names a crash when the socket is down mid-retry", () => {
		expect(bannerLabel("disconnected", "crashed")).toBe("Daemon crashed — reconnecting");
	});

	it("says Not connected while a browser attach is waiting (no host supervisor)", () => {
		expect(bannerLabel("disconnected", "starting")).toBe("Not connected");
	});

	it("names the paired computer a phone is retrying (TD-5014)", () => {
		expect(bannerLabel("reconnecting", "starting", "tower")).toBe("Connecting to tower…");
		expect(bannerTone("reconnecting", "starting")).toBe("info");
		expect(bannerLabel("connected", "connected", "tower")).toBe("Connected");
		expect(bannerLabel("reconnecting", "starting", null)).toBe("Connecting…");
	});

	it("says it can't reach the saved computer after a while, and offers to pair again (TD-5014)", () => {
		const remote = { unreachable: true };
		expect(bannerLabel("reconnecting", "starting", "tower", remote)).toBe("Can’t reach tower");
		expect(bannerLabel("reconnecting", "starting", null, remote)).toBe("Can’t reach the computer");
		expect(bannerTone("reconnecting", "starting", remote)).toBe("warning");
		expect(bannerOffersPairAgain("reconnecting", remote)).toBe(true);
		expect(bannerOffersPairAgain("reconnecting", {})).toBe(false);
		expect(bannerLabel("connected", "connected", "tower", remote)).toBe("Connected");
		expect(bannerOffersPairAgain("connected", remote)).toBe(false);
		expect(bannerOffersPairAgain("stopped", remote)).toBe(false);
	});

	it("shows a handshake note while it keeps retrying, without offering to pair (TD-5014)", () => {
		const note = "This computer can't read its paired devices right now. Trying again.";
		const remote = { unreachable: true, note };
		expect(bannerLabel("reconnecting", "starting", "tower", remote)).toBe(note);
		expect(bannerTone("reconnecting", "starting", remote)).toBe("warning");
		expect(bannerOffersPairAgain("reconnecting", remote)).toBe(false);
		expect(bannerLabel("connected", "connected", "tower", remote)).toBe("Connected");
	});

	it("says Daemon stopped on an orderly shutdown", () => {
		expect(bannerLabel("connecting", "stopping")).toBe("Daemon stopped");
		expect(bannerLabel("stopped", "stopped")).toBe("Stopped");
	});
});
