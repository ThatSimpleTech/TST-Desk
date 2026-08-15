// Greeting empty state logic (TD-1605).
//
// Pure and rune-free so the hour boundaries are unit-testable in node.
// The greeting is time-aware by *local* hour — the window is a local app;
// the clock on the user's machine is the truth we're greeting from.

/** Morning 05:00–11:59, afternoon 12:00–16:59, evening otherwise. */
export function greetingForHour(hour: number): string {
	if (hour >= 5 && hour < 12) return "Good morning";
	if (hour >= 12 && hour < 17) return "Good afternoon";
	return "Good evening";
}

/** Suggestion chips in product voice. Clicking inserts into the composer —
 *  never auto-sends (TD-1605 AC). */
export const SUGGESTIONS = [
	"Review this repo",
	"Find what's failing",
	"Explain this codebase",
] as const;
