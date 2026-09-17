// Compiler diagnostics guard the textarea semantics and stylesheet (TD-4833).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { compile } from "svelte/compiler";
import { expect, it } from "vitest";

it("compiles Composer without accessibility or unused-CSS warnings", () => {
	const filename = resolve(process.cwd(), "src/lib/components/chat/Composer.svelte");
	const result = compile(readFileSync(filename, "utf8"), { filename });
	expect(result.warnings.map((warning) => `${warning.code}: ${warning.message}`)).toEqual([]);
});
