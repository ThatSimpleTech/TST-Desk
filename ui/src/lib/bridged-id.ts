// Bridged session ids (TD-5015).
//
// The local daemon is a one-hop hub: a session on a paired computer reaches
// this window as `<raw>:<mch_id>`, where `mch_id` is the hub's own handle
// for that computer. Own sessions keep their raw uuid4 id, which can never
// match the grammar. Everything in the UI that has a local-only side effect
// (the disk, a native dialog, the last workspace, the tray) gates on
// `machineOf(id) !== null` and never on whether a path happens to exist:
// under a self-pair the remote copy's paths exist here too.
//
// Pure, so every gate and the rail can share it without importing a store.

import type { ClientMessageUnion } from "./protocol";

/** The id grammar, a protocol contract (docs/architecture.md). */
export const BRIDGED_ID_RE = /^(?<raw>[^:]+):(?<mch>mch_[0-9a-f]{12})$/;

/** The paired computer a session lives on, or null for this daemon's own. */
export function machineOf(sessionId: string | null | undefined): string | null {
  if (typeof sessionId !== "string") return null;
  return BRIDGED_ID_RE.exec(sessionId)?.groups?.mch ?? null;
}

/** True for a session on another computer. */
export function isBridged(sessionId: string | null | undefined): boolean {
  return machineOf(sessionId) !== null;
}

/** The id the owning daemon knows the session by. Own ids are returned as is. */
export function rawId(sessionId: string): string {
  return BRIDGED_ID_RE.exec(sessionId)?.groups?.raw ?? sessionId;
}

/**
 * The verbs the hub forwards for a bridged id: core's `FORWARDED_VERBS`
 * (core/tstd/peer_policy.py), pinned against protocol-fixtures.json's
 * `forwarded_verbs` by bridged-id.test.ts. Anything else naming a bridged
 * session would only earn a `not_on_remote` error, so the UI never sends it.
 */
export const BRIDGED_VERBS: ReadonlySet<string> = new Set([
  "list_sessions",
  "attach",
  "detach",
  "user_message",
  "stop_turn",
  "cancel",
  "resume",
  "approve",
  "deny",
  "always_allow",
  "run_verify",
  "deny_verify",
  "new_session",
  "fork_from",
  "set_branch",
  "set_tier",
  "set_plan",
  "set_session_preset",
  "rename_session",
  "set_session_star",
  "archive_session",
  "delete_session",
  "end_session",
  "get_instruction_stack",
  "memory_accept",
  "memory_edit",
  "memory_reject",
  "read_artifact_chunk", // TD-5143: download a sent file from another computer
]);

/** True when `msg` names a bridged session with a verb the hub won't forward.
 *  The send is then dropped here instead of asking the local daemon about a
 *  session it doesn't own. */
export function refusedForBridged(msg: ClientMessageUnion): boolean {
  const sessionId = (msg as { session_id?: unknown }).session_id;
  if (typeof sessionId !== "string" || !isBridged(sessionId)) return false;
  return !BRIDGED_VERBS.has(msg.type);
}
