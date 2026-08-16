// Cost meter formatting (TD-1802): free must read as free, and real
// spend must never be able to read as free.

import { describe, expect, it } from "vitest";
import { formatUsd } from "./cost-format";

describe("formatUsd", () => {
	it("renders a zero-price preset as $0.00", () => {
		expect(formatUsd(0)).toBe("$0.00");
	});

	it("keeps four decimals for spend that merely rounds to zero", () => {
		expect(formatUsd(0.00001)).toBe("$0.0000");
		expect(formatUsd(0.000001)).toBe("$0.0000");
	});

	it("keeps four decimals for sub-dollar spend", () => {
		expect(formatUsd(0.0037)).toBe("$0.0037");
		expect(formatUsd(0.9999)).toBe("$0.9999");
	});

	it("drops to two decimals at a dollar and above", () => {
		expect(formatUsd(1)).toBe("$1.00");
		expect(formatUsd(12.345)).toBe("$12.35");
	});
});
