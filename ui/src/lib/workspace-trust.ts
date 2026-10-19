// Workspace config trust card model (TD-5113).
//
// Pure mapping from `boundary_update.config_trust` to what the footer shows,
// plus the message that answers it. Kept free of runes so vitest drives it
// in node; the sender lives in session-status.svelte.ts.

import type { ConfigTrust, TrustWorkspaceConfig } from "./protocol";

/** `ask` while the user has not decided, `declined` once they kept the
 *  defaults, `hidden` when the config is in force or there is nothing to ask. */
export type TrustCardMode = "ask" | "declined" | "hidden";

export function trustCardMode(trust: ConfigTrust | null | undefined): TrustCardMode {
  if (trust === null || trust === undefined || trust.loosened.length === 0) return "hidden";
  if (trust.state === "pending") return "ask";
  if (trust.state === "declined") return "declined";
  return "hidden";
}

/** The answer for exactly the file the card showed: its hash goes back, so
 *  a file that changed since is refused rather than trusted unseen. */
export function trustMessage(
  sessionId: string,
  trust: ConfigTrust,
  accept: boolean,
): TrustWorkspaceConfig {
  return {
    type: "trust_workspace_config",
    session_id: sessionId,
    sha256: trust.sha256,
    trust: accept,
  };
}
