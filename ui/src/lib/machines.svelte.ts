// The computers this window's daemon has paired (TD-5015).
//
// Fed by `machine_list` (the reply to every machine verb, also pushed when a
// link's status changes). The list is asked for on connect only when the
// daemon's hello_ack carried a machine_id, so a daemon from before TD-5015 is
// never sent a verb it doesn't know: with no machines the rail and Settings
// are exactly what they were. Pure helpers live in machines.ts.

import { onEvent, onConnectionState, sendToDaemon, ws } from "./connection-status.svelte.js";
import { hub } from "./hub.svelte.js";
import { machineOf } from "./bridged-id";
import { claimInline } from "./inline-errors";
import { ADD_MACHINE_ERRORS, addMachineError, checkAddMachine, machineLabel, type AddMachineInput } from "./machines";
import type { DaemonEventUnion, MachineSummary } from "./protocol";

/** An add that neither lands nor fails in this long is shown as failed. The
 *  daemon's dial gives up well before (10 s open timeout). */
export const ADD_TIMEOUT_MS = 45_000;

export const machines = $state({
  list: [] as MachineSummary[],
  /** A machine_list has arrived on this connection. */
  loaded: false,
  /** An add_machine is in flight. */
  adding: false,
  /** Inline copy for the Add form, or null. */
  addError: null as string | null,
  /** Name of the computer the last add paired, for the form's confirmation. */
  added: null as string | null,
  /** Computer whose Remove is awaiting confirmation. */
  confirmRemoveFor: null as string | null,
});

let started = false;
let addSnapshot: Map<string, string> | null = null;
let addTimer: ReturnType<typeof setTimeout> | null = null;
/** The hub (`mid_`) the current list came from. */
let listHub: string | null = null;

/** Register the reducer and the connect-time list. Idempotent. */
export function startMachines(): () => void {
  if (started) return () => {};
  started = true;
  const offEvents = onEvent(reduce);
  const offStates = onConnectionState((state) => {
    if (state === "connected") onConnected();
    else if (state === "stopped") endAdd(null);
  });
  if (ws.state === "connected") onConnected();
  return () => {
    started = false;
    offEvents();
    offStates();
  };
}

/** Reset for tests. */
export function resetMachines(): void {
  started = false;
  machines.list = [];
  machines.loaded = false;
  machines.confirmRemoveFor = null;
  machines.added = null;
  listHub = null;
  endAdd(null);
}

/** A list belongs to the hub that sent it. Reaching another hub, or one
 *  from before TD-5015, drops it so no stale computer stays in the rail. */
function onConnected(): void {
  if (hub.machineId !== listHub) {
    machines.list = [];
    machines.loaded = false;
    machines.confirmRemoveFor = null;
  }
  listHub = hub.machineId;
  refreshMachines();
}

/** Ask for the list. False (and nothing sent) when the daemon predates
 *  TD-5015 or the socket is down. */
export function refreshMachines(): boolean {
  if (hub.machineId === null) return false;
  return sendToDaemon({ type: "list_machines" });
}

function reduce(event: DaemonEventUnion): void {
  if (event.type === "machine_list") {
    machines.list = event.machines;
    machines.loaded = true;
    if (machines.confirmRemoveFor !== null && !event.machines.some((m) => m.id === machines.confirmRemoveFor)) {
      machines.confirmRemoveFor = null;
    }
    const paired = addSnapshot === null ? null : newlyPaired(addSnapshot, event.machines);
    if (paired !== null) {
      endAdd(null);
      machines.added = paired.name;
    }
    return;
  }
  // An error another computer relayed (tagged `machine`) is never this add's.
  if (
    event.type === "error" &&
    machines.adding &&
    !event.session_id &&
    !event.machine &&
    ADD_MACHINE_ERRORS.has(event.code)
  ) {
    claimInline(event);
    endAdd(addMachineError(event.code, event.message));
    return;
  }
  if (event.type === "error" && event.code === "machine_not_found") refreshMachines();
}

/** The computer an add just paired: a new id, or a known id whose paired_at
 *  moved (re-pairing keeps the same handle). */
function newlyPaired(before: Map<string, string>, after: readonly MachineSummary[]): MachineSummary | null {
  return after.find((m) => before.get(m.id) !== m.paired_at) ?? null;
}

function endAdd(error: string | null): void {
  if (addTimer !== null) clearTimeout(addTimer);
  addTimer = null;
  addSnapshot = null;
  machines.adding = false;
  machines.addError = error;
}

/** Pair another computer from the Add form. Validation happens here, so a
 *  non-Tailscale address or a malformed link never leaves the window. */
export function addMachine(input: AddMachineInput): boolean {
  if (machines.adding) return false;
  machines.added = null;
  const check = checkAddMachine(input);
  if (!check.ok) {
    machines.addError = check.error;
    return false;
  }
  if (!sendToDaemon(check.message)) {
    machines.addError = "TST Desk isn't connected. Try again in a moment.";
    return false;
  }
  addSnapshot = new Map(machines.list.map((m) => [m.id, m.paired_at]));
  machines.adding = true;
  machines.addError = null;
  addTimer = setTimeout(() => {
    if (machines.adding) endAdd(addMachineError("machine_unreachable", ""));
  }, ADD_TIMEOUT_MS);
  return true;
}

export function clearAddFeedback(): void {
  if (!machines.adding) machines.addError = null;
  machines.added = null;
}

export function askRemoveMachine(id: string): void {
  machines.confirmRemoveFor = id;
}

export function cancelRemoveMachine(): void {
  machines.confirmRemoveFor = null;
}

/** Forget a computer; its rows leave the next session_list. */
export function removeMachine(id: string): boolean {
  machines.confirmRemoveFor = null;
  return sendToDaemon({ type: "remove_machine", machine: id });
}

/** "Try now": dial again without waiting out the backoff. */
export function reconnectMachine(id: string): boolean {
  return sendToDaemon({ type: "reconnect_machine", machine: id });
}

/** The listed computer with this handle, or null. */
export function machineById(id: string | null): MachineSummary | null {
  if (id === null) return null;
  return machines.list.find((m) => m.id === id) ?? null;
}

/** The computer a session lives on: null for this computer's own (and for a
 *  handle the list doesn't name yet). */
export function machineForSession(sessionId: string | null | undefined): MachineSummary | null {
  return machineById(machineOf(sessionId));
}

/** False only while a bridged session's computer is listed and not
 *  connected. Own sessions are always up; an unlisted handle is left to the
 *  hub, which answers machine_unreachable itself. */
export function sessionMachineUp(sessionId: string | null | undefined): boolean {
  const machine = machineForSession(sessionId);
  return machine === null || machine.status === "connected";
}

/** The label a computer is shown under, by handle; null when unlisted. */
export function nameOfMachine(id: string): string | null {
  const machine = machineById(id);
  return machine === null ? null : machineLabel(machine, machines.list);
}

/** The name a bridged session's computer is shown under, or null for own
 *  sessions. An unlisted handle reads "another computer". */
export function machineNameFor(sessionId: string | null | undefined): string | null {
  const id = machineOf(sessionId);
  if (id === null) return null;
  return nameOfMachine(id) ?? "another computer";
}
