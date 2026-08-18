import { describe, expect, it } from "vitest";
import { bannerLabel, bannerTone } from "./connection-banner";

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

	it("says Daemon stopped on an orderly shutdown", () => {
		expect(bannerLabel("connecting", "stopping")).toBe("Daemon stopped");
		expect(bannerLabel("stopped", "stopped")).toBe("Stopped");
	});
});
