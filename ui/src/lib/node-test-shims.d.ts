// Minimal ambient node declarations for test files that touch the
// filesystem (TD-1404's baseline recorder). The app tree is browser-typed
// and @types/node is deliberately not a dependency; only
// timeline-bench.test.ts relies on these.
declare module "node:fs" {
	export function readFileSync(path: string, encoding: string): string;
	export function writeFileSync(path: string, data: string): void;
}

declare module "node:path" {
	export function resolve(...segments: string[]): string;
}

declare const process: {
	cwd(): string;
	env: Record<string, string | undefined>;
};
