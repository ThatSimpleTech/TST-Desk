// Compiler diagnostics guard the textarea semantics and stylesheet (TD-4833).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { compile } from "svelte/compiler";
import { expect, it } from "vitest";

it.each([
	"src/lib/components/chat/Composer.svelte",
	"src/lib/components/chat/ModelControls.svelte",
	"src/lib/components/TitleBar.svelte",
])("compiles %s without accessibility or unused-CSS warnings", (filename) => {
	const path = resolve(process.cwd(), filename);
	const result = compile(readFileSync(path, "utf8"), { filename: path });
	expect(result.warnings.map((warning) => `${warning.code}: ${warning.message}`)).toEqual([]);
});
