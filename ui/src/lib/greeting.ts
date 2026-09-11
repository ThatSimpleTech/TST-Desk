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

export type GreetingEngine = "native" | "grok";

/** Empty-chat subtitle: workspace, the engine that will answer, and the
 *  model identity for that engine. Omits anything the daemon has not
 *  named — the UI never invents a slug (AGENTS §6). */
export function greetingContext(parts: {
	workspace?: string | null;
	engine?: GreetingEngine | null;
	tier?: string | null;
	slug?: string | null;
	grokMode?: string | null;
	grokModel?: string | null;
}): string {
	const bits: string[] = [];
	const workspace = parts.workspace?.trim() ?? "";
	if (workspace) bits.push(workspace);

	if (parts.engine === "grok") {
		bits.push("grok");
		const model = parts.grokModel?.trim() ?? "";
		if (model) bits.push(model);
		else {
			const mode = parts.grokMode?.trim() ?? "";
			if (mode) bits.push(mode);
		}
		return bits.join(" · ");
	}

	if (parts.engine === "native") bits.push("native");
	const tier = parts.tier?.trim() ?? "";
	if (tier) bits.push(tier);
	const slug = parts.slug?.trim() ?? "";
	if (slug) bits.push(slug);
	return bits.join(" · ");
}
