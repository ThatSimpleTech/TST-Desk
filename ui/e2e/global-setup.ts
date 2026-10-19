import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const uiRoot = path.resolve(here, "..");
const runtimeFile = path.join(here, ".runtime.json");
const pidFile = path.join(here, ".daemon.pid");

/** Start the mock daemon before Vite. Playwright reads `.runtime.json`. */
export default async function globalSetup(): Promise<() => Promise<void>> {
	mkdirSync(here, { recursive: true });
	const child = spawn(
		"uv",
		["run", "python", "-m", "tstd.e2e_ui", "--runtime-file", runtimeFile],
		{
			cwd: path.resolve(uiRoot, "..", "core"),
			stdio: ["ignore", "pipe", "pipe"],
		},
	);
	const stderr: Buffer[] = [];
	child.stderr?.on("data", (chunk: Buffer) => stderr.push(chunk));
	const ready = await waitForLine(child, 20_000);
	if (!ready) {
		const detail = Buffer.concat(stderr).toString("utf8").slice(-2000);
		throw new Error(`UI walk daemon did not start.\n${detail}`);
	}
	writeFileSync(pidFile, String(child.pid ?? ""), "utf8");
	return async () => {
		if (child.pid !== undefined) {
			child.kill("SIGTERM");
		}
	};
}

function waitForLine(child: ChildProcess, timeoutMs: number): Promise<boolean> {
	return new Promise((resolve) => {
		let buf = "";
		const timer = setTimeout(() => resolve(false), timeoutMs);
		const onData = (chunk: Buffer) => {
			buf += chunk.toString("utf8");
			if (buf.includes("\n")) {
				clearTimeout(timer);
				resolve(true);
			}
		};
		child.stdout?.on("data", onData);
		child.once("exit", () => {
			clearTimeout(timer);
			resolve(false);
		});
	});
}
