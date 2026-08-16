// Cost meter formatting (TD-1802).
//
// Pure and rune-free so the rounding boundaries are unit-testable in node.

/** Render a dollar amount for the cost meter.
 *
 *  Exactly zero reads `$0.00` — on a local preset every price is 0.00 and
 *  `$0.0000` reads like a value too small to show rather than free. The
 *  test is exact equality, never a rounding window: real spend that
 *  rounds to `0.0000` keeps four decimals, so the meter can never
 *  display "free" for money actually spent.
 *
 *  Above that, sub-dollar amounts keep four decimals because agent turns
 *  cost fractions of a cent; a dollar or more drops to the usual two.
 */
export function formatUsd(n: number): string {
	if (n === 0) return '$0.00';
	return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}
