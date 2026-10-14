// Minimal ambient node declarations for test files that touch the
// filesystem (TD-1404's baseline recorder, TD-5001's AppShell read). The
// app tree is browser-typed and @types/node is deliberately not a
// dependency.
declare module "node:fs" {
	export function readFileSync(path: string, encoding: string): string;
	export function writeFileSync(path: string, data: string): void;
	export function readdirSync(
		path: string,
		options: { withFileTypes: true },
	): { name: string; isDirectory(): boolean; isFile(): boolean }[];
}

declare module "node:path" {
	export function resolve(...segments: string[]): string;
	export function dirname(path: string): string;
}

declare module "node:url" {
	export function fileURLToPath(url: string | URL): string;
}

declare const process: {
	cwd(): string;
	env: Record<string, string | undefined>;
};
