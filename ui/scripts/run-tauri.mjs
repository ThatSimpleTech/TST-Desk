/** Cross-platform tauri CLI wrapper — cmd.exe cannot parse `FOO=bar cmd`. */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const uiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bin = path.join(
	uiRoot,
	"node_modules",
	".bin",
	process.platform === "win32" ? "tauri.cmd" : "tauri",
);
const child = spawn(bin, process.argv.slice(2), {
	cwd: uiRoot,
	env: { ...process.env, TAURI_APP_PATH: path.join(uiRoot, "..", "shell") },
	stdio: "inherit",
	shell: process.platform === "win32",
});
child.on("exit", (code) => process.exit(code ?? 1));
