import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { ICONS } from "./icons";

// Runtime objects have already discarded duplicate keys, so inspect the source
// map as well: a merge must not silently replace a glyph before tests see it.
describe("shared icon catalogue", () => {
	it("declares every icon name once, including the microphone", () => {
		const source = ts.createSourceFile(
			"icons.ts",
			readFileSync(resolve(process.cwd(), "src/lib/icons.ts"), "utf8"),
			ts.ScriptTarget.Latest,
			true,
		);
		const names: string[] = [];
		function visit(node: ts.Node): void {
			if (ts.isPropertyAssignment(node)) {
				const name = node.name;
				if (ts.isIdentifier(name) || ts.isStringLiteral(name)) names.push(name.text);
			}
			ts.forEachChild(node, visit);
		}
		visit(source);
		expect(names.filter((name) => name === "mic")).toHaveLength(1);
		expect(names.length).toBe(new Set(names).size);
		expect([...names].sort()).toEqual(Object.keys(ICONS).sort());
	});
});
