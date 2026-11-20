// TD-5239: a model in the catalog can name its engine, and a chat keeps the
// engine it started on. The daemon sends which models name one
// (`setup_state.preset_engines`) and each chat's engine; these say what the
// picker shows for them. The daemon still refuses a switch across engines;
// the picker only says so before the click.

export type Engine = "native" | "grok";

export type PresetEngines = Readonly<Record<string, Engine>>;

/** The mark on a model's row: "Grok" for one that runs on the Grok CLI. */
export function engineMark(preset: string, engines: PresetEngines): string {
	return engines[preset] === "grok" ? "Grok" : "";
}

/** The engine a new chat on `active` would run on: its own, else the setting. */
export function newChatEngine(
	active: string | null,
	engines: PresetEngines,
	setting: Engine,
): Engine {
	return (active !== null ? engines[active] : undefined) ?? setting;
}

/** Why the chat cannot switch to `preset`, or null. A model that names no
 *  engine suits a chat on either. */
export function engineClash(
	preset: string,
	engines: PresetEngines,
	chatEngine: Engine | null,
): string | null {
	const wanted = engines[preset];
	if (wanted === undefined || chatEngine === null || wanted === chatEngine) return null;
	return wanted === "grok"
		? "Runs on the Grok CLI. A chat keeps the engine it started on, so start a new chat to use it."
		: "Runs on TST Desk's own loop. This chat is on the Grok CLI, so start a new chat to use it.";
}
