// Other computers in the rail and in Settings → Computers (TD-5015).
//
// Pure: the sections the rail draws, the names and status words they show,
// and the Add-a-computer form's validation. "machine" is the code and
// protocol word; "computer" is what the copy says ("computer" in code
// already means computer use). machines.svelte.ts holds the live list.

import { machineOf } from "./bridged-id";
import { relativeWords } from "./device-words";
import { pairLinkFromTyped, parsePairLink } from "./pairing";
import type { AddMachine, MachineStatus, MachineSummary } from "./protocol";
import { railOrder, groupRowsByRecency } from "./rail";

/** The local section's title on a desktop. A phone uses its hub's name. */
export const THIS_COMPUTER = "This computer";

export interface ComputerSection<T> {
  /** "local", or the hub's `mch_` handle for that computer. */
  id: string;
  /** Null for this computer's own rows (or a computer the list doesn't name yet). */
  machine: MachineSummary | null;
  title: string;
  rows: T[];
}

/**
 * The rail's per-computer sections, in order: this computer first, then one
 * per paired computer in the hub's order. With no paired computers there is
 * one section holding every row, and the rail renders exactly what it did
 * before TD-5015. Rows of a computer the list doesn't name yet (a
 * session_list that beat machine_list) still get a section, so a row is
 * never hidden.
 */
export function railSections<T extends { sessionId: string }>(
  rows: readonly T[],
  machines: readonly MachineSummary[],
  localTitle: string = THIS_COMPUTER,
): ComputerSection<T>[] {
  if (machines.length === 0) return [{ id: "local", machine: null, title: localTitle, rows: [...rows] }];
  const byMachine = new Map<string, T[]>();
  const local: T[] = [];
  for (const row of rows) {
    const mch = machineOf(row.sessionId);
    if (mch === null) {
      local.push(row);
      continue;
    }
    const list = byMachine.get(mch) ?? [];
    list.push(row);
    byMachine.set(mch, list);
  }
  const sections: ComputerSection<T>[] = [{ id: "local", machine: null, title: localTitle, rows: local }];
  for (const machine of machines) {
    sections.push({ id: machine.id, machine, title: machineLabel(machine, machines), rows: byMachine.get(machine.id) ?? [] });
    byMachine.delete(machine.id);
  }
  for (const [id, orphans] of byMachine) {
    sections.push({ id, machine: null, title: "Another computer", rows: orphans });
  }
  return sections;
}

/** Keyboard order across sections: each section's recency groups in turn,
 *  so ⌘⌥↑/↓ walks the rail top to bottom as drawn. */
export function sessionOrderAcross<T extends { sessionId: string; updatedAt: string; starred: boolean }>(
  rows: readonly T[],
  machines: readonly MachineSummary[],
): string[] {
  return railSections(rows, machines).flatMap((section) => railOrder(groupRowsByRecency(section.rows)));
}

/** The rows the collapsed strip draws, in the expanded rail's section order.
 *  With no paired computers this is `rows` unchanged. */
export function stripRows<T extends { sessionId: string }>(rows: readonly T[], machines: readonly MachineSummary[]): T[] {
  if (machines.length === 0) return [...rows];
  return railSections(rows, machines).flatMap((section) => section.rows);
}

/**
 * The name a computer is shown under. A self-pair says so; two computers
 * with one name get their address, and the port too when that also matches
 * (two daemons on one box). A phone is never sent addresses, so there the
 * hub's handle tells them apart.
 */
export function machineLabel(machine: MachineSummary, all: readonly MachineSummary[]): string {
  if (machine.same_computer) return `${machine.name} (this computer)`;
  const twins = all.filter((other) => other.id !== machine.id && other.name === machine.name);
  if (twins.length === 0) return machine.name;
  if (machine.host !== null) {
    if (twins.every((other) => other.host !== machine.host)) return `${machine.name} (${machine.host})`;
    if (machine.port !== null) return `${machine.name} (${machine.host}:${machine.port})`;
  }
  return `${machine.name} (${machine.id.slice(-4)})`;
}

/** One-word status, for the dot's label and the Settings list. */
export const STATUS_WORDS: Record<MachineStatus, string> = {
  connected: "Connected",
  connecting: "Connecting…",
  unreachable: "Not reachable",
  refused: "Not paired anymore",
  locked: "Keychain locked",
  outdated: "Needs an update",
};

export type MachineTone = "ok" | "info" | "warn" | "muted";

export function statusTone(status: MachineStatus): MachineTone {
  if (status === "connected") return "ok";
  if (status === "connecting") return "info";
  if (status === "unreachable") return "muted";
  return "warn";
}

/** "last seen 2 hours ago", or null when the hub has never reached it. */
export function lastSeenWords(machine: MachineSummary, now: number = Date.now()): string | null {
  return machine.last_seen === null ? null : `last seen ${relativeWords(machine.last_seen, now)}`;
}

/** The line under a section header, or null while connected. */
export function statusLine(machine: MachineSummary, now: number = Date.now()): string | null {
  const seen = lastSeenWords(machine, now);
  switch (machine.status) {
    case "connected":
      return null;
    case "connecting":
      return STATUS_WORDS.connecting;
    case "unreachable":
      return seen === null ? STATUS_WORDS.unreachable : `${STATUS_WORDS.unreachable} · ${seen}`;
    case "refused":
      return `${machine.name} no longer knows this computer. Pair again or remove it.`;
    case "locked":
      return "The keychain is locked, so its key can't be read. Unlock it, then Try now.";
    case "outdated":
      return `${machine.name} needs a newer TST Desk. Update both computers.`;
  }
}

/** Whether the rail greys this computer's rows. Last-known rows stay listed
 *  and open; only the composer waits for the computer. */
export function isOffline(machine: MachineSummary | null): boolean {
  return machine !== null && machine.status !== "connected";
}

/** Copy for Remove's confirm step. */
export function removeConfirmCopy(machine: MachineSummary): string {
  const base = `Remove ${machine.name}? Its sessions leave this list.`;
  if (machine.status === "connected") return base;
  return `${base} ${machine.name} still lists this computer until it's removed there.`;
}

/** The composer's notice while a bridged session's computer is not connected. */
export function unreachableNotice(name: string): string {
  return `${name} isn't reachable. This session opens when it is.`;
}

/** An approval card's note while its computer is not connected: the hub
 *  refuses a decision it can't deliver, so the buttons wait too. */
export function approvalWaitCopy(name: string): string {
  return `${name} isn't reachable. Approve or deny once it is.`;
}

/** "+ New here" with nothing to anchor on. */
export function firstSessionCopy(name: string): string {
  return `Start the first session on ${name}; it shows up here.`;
}

/** Workspace-keyed panes for a bridged session (charter, commands, pins,
 *  instructions, memory files): their verbs are unscoped and would ask this
 *  computer about a path on that one. */
export function staysOnCopy(name: string): string {
  return `These stay on ${name}.`;
}

/** Settings on a phone: the hub alone pairs and removes computers. */
export function readOnlyCopy(hubName: string): string {
  return `Add or remove computers in Settings on ${hubName}.`;
}

/** The phone's pair-offer note: a phone paired to a hub sees its computers too. */
export function alsoSeesCopy(names: readonly string[]): string | null {
  if (names.length === 0) return null;
  return `This phone will also see sessions on: ${names.join(", ")}.`;
}

/** The pair note for a hub's list. A self-pair is left out: its sessions
 *  are this computer's own. */
export function alsoSeesFor(machines: readonly MachineSummary[]): string | null {
  return alsoSeesCopy(machines.filter((m) => !m.same_computer).map((m) => m.name));
}

/** The distinct workspaces a computer's rows name, newest first: what
 *  "+ New here" offers, each with an anchor session in it. */
export function knownWorkspaces<T extends { sessionId: string; workspacePath: string; updatedAt: string }>(
  rows: readonly T[],
): Array<{ path: string; anchor: string }> {
  const newest = new Map<string, T>();
  for (const row of rows) {
    const seen = newest.get(row.workspacePath);
    if (seen === undefined || row.updatedAt > seen.updatedAt) newest.set(row.workspacePath, row);
  }
  return [...newest.values()]
    .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0))
    .map((row) => ({ path: row.workspacePath, anchor: row.sessionId }));
}

// ── Add a computer ─────────────────────────────────────────────────────

export type AddMachineInput = { link: string } | { address: string; code: string };

export type AddMachineCheck = { ok: true; message: AddMachine } | { ok: false; error: string };

/** Validate the form the way a phone validates a scan (pairing.ts), so a
 *  non-Tailscale address never leaves the window. The pasted link goes as
 *  is; a typed code goes normalized. */
export function checkAddMachine(input: AddMachineInput): AddMachineCheck {
  if ("link" in input) {
    const link = input.link.trim();
    if (link === "") return { ok: false, error: "Paste the tstdesk://pair link from the other computer." };
    const parsed = parsePairLink(link);
    return parsed.ok ? { ok: true, message: { type: "add_machine", link } } : { ok: false, error: parsed.error };
  }
  const parsed = pairLinkFromTyped(input.address, input.code);
  if (!parsed.ok) return { ok: false, error: parsed.error };
  const { host, port, code } = parsed.link;
  return { ok: true, message: { type: "add_machine", host, port, code } };
}

/** The add-a-computer errors the form shows inline. */
export const ADD_MACHINE_ERRORS: ReadonlySet<string> = new Set([
  "pair_failed",
  "pair_locked",
  "machine_unreachable",
  "not_tailscale",
  "machine_limit",
  "machine_busy",
  "keychain_locked",
]);

const ADD_MACHINE_COPY: Record<string, string> = {
  pair_failed: "That code didn't work. Start pairing again on the other computer.",
  pair_locked: "Too many wrong codes. Start pairing again on the other computer.",
  machine_unreachable:
    "Couldn't reach that computer. Check it is on, on Tailscale, and has remote attach turned on.",
  not_tailscale: "That address isn't on Tailscale, so TST Desk won't connect to it.",
  machine_limit: "This computer has paired as many computers as it can. Remove one first.",
  machine_busy: "Already adding a computer. Wait for that to finish.",
  keychain_locked: "The keychain is locked, so the key couldn't be saved. Unlock it and try again.",
};

/** Inline copy for an add-a-computer error code; the daemon's own message
 *  for anything else. */
export function addMachineError(code: string, message: string): string {
  return ADD_MACHINE_COPY[code] ?? message;
}
