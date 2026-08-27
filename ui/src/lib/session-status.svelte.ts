// Session-scoped state store (TD-1006).
//
// Reduces the daemon event stream into the fields the title bar shows:
// session id + workspace path, state indicator, live cost with a per-tier
// breakdown, the current tier + slugs, and the boundary ("wall").
//
// v0.1 is single-workspace per window: the first session_state event we see
// adopts that session unless this webview is bound with ?bind_session= (TD-4703).
// Multi-session routing uses window-scoped binds and the rail's open-in-window.
//
// Pure TypeScript — no Tauri imports — so vitest can drive the reducer.

import { DEFAULT_ATTACHMENT_LIMITS } from "./attachments";
import type { ProtocolClient } from "./client";
import { persistLastWorkspace } from "./last-workspace";
import type { AttachmentLimits, BoundaryUpdate, DaemonEventUnion } from "./protocol";
import { windowBindSessionId } from "./window-bind";

export type SessionIndicator =
  | "none"
  | "idle"
  | "running"
  | "awaiting_approval"
  | "paused"
  | "complete"
  | "failed"
  | "cancelled"
  | "interrupted";

export const session = $state({
  sessionId: null as string | null,
  /** Path we opened (client-side knowledge; events don't echo it). */
  workspacePath: null as string | null,
  state: "none" as SessionIndicator,
  reason: null as string | null,
  cost: {
    turn: 0,
    session: 0,
    total: 0,
    classifier: 0,
    byTier: {} as Record<string, number>,
  },
  boundary: null as Pick<
    BoundaryUpdate,
    "writable_paths" | "allowed_commands" | "network" | "spend_usd" | "wall_clock_hours" | "max_iterations" | "source"
  > | null,
  /** Attachment caps for the composer (TD-1709). Held separately from
   *  `boundary` because it is never null: the composer has to judge a
   *  dropped file before any session is bound, and the shipped defaults are
   *  what the daemon would apply anyway. A daemon too old to send them
   *  leaves these in place rather than blanking the gate. */
  attachmentLimits: { ...DEFAULT_ATTACHMENT_LIMITS } as AttachmentLimits,
  tier: "brain" as "brain" | "worker" | "validator",
  tierOverride: null as "brain" | "worker" | "validator" | null,
  modelSlugs: {} as Record<string, string>,
  /** Preset this session opened with (TD-1720). Empty until tier_state. */
  preset: "" as string,
  /** Tier → hostname:port the daemon will call (TD-1720). */
  hosts: {} as Record<string, string>,
  /** Plan mode (TD-4603). False until a tier_state says otherwise. */
  planMode: false,
  /** A turn is owed (TD-1721). The daemon refuses a preset switch then. */
  turnActive: false,
});

let client: ProtocolClient | null = null;
/** The path of an open_workspace we've sent but not yet seen answered. */
let pendingPath: string | null = null;

/** Hand the store the live client. Called by connection-status. */
export function bindClient(c: ProtocolClient | null): void {
  client = c;
}

/** Clear everything (called when the connection is torn down). */
export function resetSession(): void {
  session.sessionId = null;
  session.workspacePath = null;
  session.state = "none";
  session.reason = null;
  session.cost = { turn: 0, session: 0, total: 0, classifier: 0, byTier: {} };
  session.boundary = null;
  session.attachmentLimits = { ...DEFAULT_ATTACHMENT_LIMITS };
  session.tier = "brain";
  session.tierOverride = null;
  session.modelSlugs = {};
  session.preset = "";
  session.hosts = {};
  session.planMode = false;
  session.turnActive = false;
  pendingPath = null;
}

/** Reduce one validated daemon event into the store. */
export function ingestEvent(event: DaemonEventUnion): void {
  if (event.type === "session_state") {
    const bind = windowBindSessionId();
    if (bind !== null && event.session_id !== bind) return;
    // Adopt the first session we hear of. The open_workspace reply is a
    // session_state event, so a plain open lands here.
    if (session.sessionId === null) {
      session.sessionId = event.session_id;
      if (pendingPath !== null) {
        session.workspacePath = pendingPath;
        pendingPath = null;
      }
      // Follow the session: replay starts at the client's last seen seq,
      // so nothing from open (boundary/tier) is lost.
      client?.attach(event.session_id);
    }
    if (event.session_id !== session.sessionId) return;
    session.state = event.state;
    session.reason = event.reason ?? null;
    return;
  }

  // Remaining cases are session-scoped: ignore anything not ours.
  if (!("session_id" in event) || event.session_id !== session.sessionId) return;

  switch (event.type) {
    case "boundary_update":
      session.boundary = {
        writable_paths: event.writable_paths,
        allowed_commands: event.allowed_commands,
        network: event.network,
        spend_usd: event.spend_usd,
        wall_clock_hours: event.wall_clock_hours,
        max_iterations: event.max_iterations,
        source: event.source,
      };
      // TD-1709: an older daemon omits these; keeping the last known caps
      // beats standing the composer's early refusal down to nothing.
      if (event.attachments !== undefined) session.attachmentLimits = event.attachments;
      break;
    case "tier_state":
      session.tier = event.tier;
      session.tierOverride = event.override ?? null;
      session.modelSlugs = event.model_slugs;
      session.preset = event.preset ?? "";
      session.hosts = event.hosts ?? {};
      session.planMode = event.plan ?? false;
      break;
    case "user_turn":
      session.turnActive = true;
      break;
    case "turn_complete":
      session.turnActive = false;
      break;
    case "cost_update":
      session.cost = {
        turn: event.turn_cost,
        session: event.session_cost,
        total: event.total_cost,
        classifier: event.classifier_cost,
        byTier: { ...event.cost_by_tier },
      };
      break;
  }
}

/** Ask the daemon to open a workspace (the title bar's picker action). */
export function openWorkspace(path: string): void {
  pendingPath = path;
  client?.openWorkspace(path);
  void persistLastWorkspace(path);
}

/** Re-target this store at another live session (TD-1701 rail selection).
 *
 *  The rail knows the session's id, workspace, and last-reported state from
 *  `session_list`; the attach replay then repopulates everything the log
 *  carries (boundary, tier, cost, final state) — so session-derived fields
 *  reset here instead of showing the previous session's numbers under the
 *  new id. Attaching itself stays with the chat store, which owns the
 *  attach/detach pairing. No-op when the store already follows this id.
 */
export function focusSession(
  sessionId: string,
  state: SessionIndicator,
  workspacePath?: string,
): void {
  if (session.sessionId === sessionId) return;
  session.sessionId = sessionId;
  if (workspacePath !== undefined) {
    session.workspacePath = workspacePath;
    void persistLastWorkspace(workspacePath);
  }
  session.state = state;
  session.reason = null;
  session.cost = { turn: 0, session: 0, total: 0, classifier: 0, byTier: {} };
  session.boundary = null;
  session.attachmentLimits = { ...DEFAULT_ATTACHMENT_LIMITS };
  session.tier = "brain";
  session.tierOverride = null;
  session.modelSlugs = {};
  session.preset = "";
  session.hosts = {};
  session.planMode = false;
  session.turnActive = false;
  pendingPath = null;
}

/** Follow a session that moved to another project (TD-1715).
 *
 *  Only the workspace changes — same session, same conversation — so this is
 *  deliberately not `focusSession`, which resets the session-derived fields.
 *  Ignored unless the store is actually following that session. */
export function retargetWorkspace(sessionId: string, workspacePath: string): void {
  if (session.sessionId !== sessionId) return;
  session.workspacePath = workspacePath;
  void persistLastWorkspace(workspacePath);
}

/** Pin a tier on the active session (a chip click). */
export function setTier(tier: "brain" | "worker" | "validator"): void {
  if (session.sessionId === null) return;
  client?.setTier(session.sessionId, tier);
}

/** Turn plan mode on or off. The title bar reflects the next tier_state. */
export function setPlan(on: boolean): void {
  if (session.sessionId === null) return;
  client?.setPlan(session.sessionId, on);
}

/** Retarget this session at a catalog preset. The title bar follows tier_state. */
export function setSessionPreset(name: string): void {
  if (session.sessionId === null) return;
  client?.setSessionPreset(session.sessionId, name);
}

/** Active slug and host for the title-bar pill (TD-1720). */
export function liveModelLabel(
  tier: string,
  slugs: Record<string, string>,
  hosts: Record<string, string>,
): string {
  const slug = slugs[tier] ?? "";
  const host = hosts[tier] ?? "";
  if (slug && host) return `${slug} · ${host}`;
  return slug || host;
}

/** Display name for the workspace row: basename of the path. */
export function workspaceName(path: string): string {
  const parts = path.split(/[\\/]/).filter((p) => p.length > 0);
  return parts[parts.length - 1] ?? path;
}
