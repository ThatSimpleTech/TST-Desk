// Shared wall for `read_text_file` (TD-3202).
//
// Tauri 2 looks command arguments up in camelCase. A snake_case
// `session_id` is treated as absent, so the session dir is never
// opened and every screen frame and session-artifact preview is refused.
// The refusal is a string, not an Error, so callers must not drop it.

// The extra index signature is what makes this assignable to Tauri's
// `InvokeArgs` (a `Record<string, unknown>`): a named interface without one is
// not. The three declared keys keep the camelCase contract documented.
export interface ReadTextFileCommand {
	path: string;
	workspace: string | null;
	sessionId: string | null;
	[key: string]: unknown;
}

export function readTextFileCommand(args: ReadTextFileCommand): ReadTextFileCommand {
	return {
		path: args.path,
		workspace: args.workspace,
		sessionId: args.sessionId,
	};
}

/** Host commands reject with a string. Keep that text. */
export function hostCommandError(err: unknown, fallback: string): string {
	if (typeof err === "string" && err.length > 0) return err;
	if (err instanceof Error && err.message.length > 0) return err.message;
	return fallback;
}
