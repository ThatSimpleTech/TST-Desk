// Pure helpers for the project home's Instructions column (TD-2802).

export type InstructionKind = "agents" | "claude" | "rule";

export interface InstructionFile {
	path: string;
	name: string;
	kind: InstructionKind;
}

/** Empty-state copy — points at the steering guide, not a blank card. */
export function instructionEmptyCopy(): string {
	return "No instructions yet. Put an AGENTS.md at the project root — see docs/steering.md.";
}

/** The root steering file, if the column has one (AGENTS.md or CLAUDE.md). */
export function rootInstruction(files: readonly InstructionFile[]): InstructionFile | null {
	return files.find((f) => f.kind === "agents" || f.kind === "claude") ?? null;
}

export function ruleInstructions(files: readonly InstructionFile[]): InstructionFile[] {
	return files.filter((f) => f.kind === "rule");
}
