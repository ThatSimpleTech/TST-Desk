// Pure helpers for the project home's Charter column (TD-4002).

import type { CharterFields, CharterNetwork } from "./protocol";

/** Empty-state copy — points at spec §12.4, not a blank YAML card. */
export function charterEmptyCopy(): string {
	return (
		"No charter yet. Spec §12.4 is the contract an autonomous run starts " +
		"against: objective, definition of done, source of truth, boundary, " +
		"caps, and stop conditions. Write it here as the human — the agent cannot."
	);
}

/** Column lede. Names the spec section even when a charter is present. */
export function charterLedeCopy(): string {
	return "Signed before an autonomous run starts (spec §12.4). The agent cannot write this file.";
}

/** Start-button confirm. The exact phrase is an acceptance criterion. */
export function containerCopy(): string {
	return "this runs in a container";
}

export function startConfirmCopy(): string {
	return (
		"Sign this charter and start. The wall and caps below are what the run " +
		"is bound to. This runs in a container."
	);
}

export function emptyCharterDraft(): CharterFields {
	return {
		objective: "",
		definition_of_done: [""],
		source_of_truth: [""],
		boundary: {
			writable_paths: ["**"],
			allowed_commands: [""],
			network: "deny",
		},
		caps: {
			spend_usd: 25,
			wall_clock_hours: 8,
			max_iterations: 200,
			shell_memory_mb: null,
		},
		stop_conditions: [""],
	};
}

function nonempty(entries: string[]): string[] {
	return entries.map((entry) => entry.trim()).filter((entry) => entry.length > 0);
}

/** The shell memory cap a charter sets, or null when it leaves it to the workspace (TD-5116). */
export function charterShellMemory(caps: CharterFields["caps"]): number | null {
	const mb = caps.shell_memory_mb;
	return typeof mb === "number" && Number.isFinite(mb) ? mb : null;
}

/** Sign-card line for the shell memory cap (TD-5116). */
export function shellMemoryCopy(caps: CharterFields["caps"]): string {
	const mb = charterShellMemory(caps);
	return mb === null
		? "not set: .tst/config.yaml's caps.shell_memory_mb, else half of RAM"
		: `${mb} MB per command`;
}

export function networkIsDeny(network: CharterNetwork): network is "deny" {
	return network === "deny";
}

/** Mapping the daemon parse_charter's. Blank list rows are dropped. */
export function charterPayload(fields: CharterFields): Record<string, unknown> {
	const hosts = networkIsDeny(fields.boundary.network)
		? "deny"
		: nonempty(fields.boundary.network);
	return {
		objective: fields.objective,
		definition_of_done: nonempty(fields.definition_of_done),
		source_of_truth: nonempty(fields.source_of_truth),
		boundary: {
			writable_paths: nonempty(fields.boundary.writable_paths),
			allowed_commands: nonempty(fields.boundary.allowed_commands),
			network: hosts,
		},
		caps: capsPayload(fields.caps),
		stop_conditions: nonempty(fields.stop_conditions),
	};
}

/** Caps as the daemon takes them. An unset shell memory cap is left out, so
 *  the run keeps the workspace's (TD-5116); a set one is never dropped. */
function capsPayload(caps: CharterFields["caps"]): Record<string, number> {
	const out: Record<string, number> = {
		spend_usd: caps.spend_usd,
		wall_clock_hours: caps.wall_clock_hours,
		max_iterations: caps.max_iterations,
	};
	const mb = charterShellMemory(caps);
	if (mb !== null) out.shell_memory_mb = mb;
	return out;
}

function yamlList(items: string[]): string {
	if (items.length === 0) return "[]";
	return items.map((item) => `  - ${JSON.stringify(item)}`).join("\n");
}

function yamlNetwork(network: CharterNetwork): string {
	if (network === "deny") return "deny";
	return `\n${yamlList(network)}`;
}

/** Preview only — the daemon is the writer. */
export function previewCharterYaml(fields: CharterFields, notes: string): string {
	const payload = charterPayload(fields);
	const boundary = payload.boundary as {
		writable_paths: string[];
		allowed_commands: string[];
		network: CharterNetwork;
	};
	const caps = payload.caps as CharterFields["caps"];
	const sot = payload.source_of_truth as string[];
	const dod = payload.definition_of_done as string[];
	const stops = payload.stop_conditions as string[];
	const body = [
		"---",
		`objective: ${JSON.stringify(payload.objective)}`,
		"definition_of_done:",
		yamlList(dod) === "[]" ? "  []" : yamlList(dod),
		"source_of_truth:",
		yamlList(sot) === "[]" ? "  []" : yamlList(sot),
		"boundary:",
		"  writable_paths:",
		yamlList(boundary.writable_paths) === "[]" ? "    []" : yamlList(boundary.writable_paths),
		"  allowed_commands:",
		yamlList(boundary.allowed_commands) === "[]" ? "    []" : yamlList(boundary.allowed_commands),
		`  network: ${yamlNetwork(boundary.network)}`,
		"caps:",
		`  spend_usd: ${caps.spend_usd}`,
		`  wall_clock_hours: ${caps.wall_clock_hours}`,
		`  max_iterations: ${caps.max_iterations}`,
		...(caps.shell_memory_mb === undefined ? [] : [`  shell_memory_mb: ${caps.shell_memory_mb}`]),
		"stop_conditions:",
		yamlList(stops) === "[]" ? "  []" : yamlList(stops),
		"---",
	].join("\n");
	const trimmed = notes.trim();
	return trimmed === "" ? `${body}\n` : `${body}\n\n${trimmed}\n`;
}

export function listWithBlank(values: string[]): string[] {
	return values.length === 0 ? [""] : values;
}

export function charterFromEvent(fields: CharterFields): CharterFields {
	return {
		objective: fields.objective,
		definition_of_done: listWithBlank(fields.definition_of_done),
		source_of_truth: listWithBlank(fields.source_of_truth),
		boundary: {
			writable_paths: listWithBlank(fields.boundary.writable_paths),
			allowed_commands: listWithBlank(fields.boundary.allowed_commands),
			network: fields.boundary.network,
		},
		caps: { ...fields.caps },
		stop_conditions: listWithBlank(fields.stop_conditions),
	};
}
