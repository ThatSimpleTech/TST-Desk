// The daemon this window talks to, as its hello_ack names it (TD-5015).
//
// Its own leaf module so the connection store can set it and the rail,
// Settings and notices can read it without importing the connection (the
// stores that read it are often tested with that module mocked). An old
// daemon sends neither field: machineId stays null, and nothing new is ever
// sent to it.

import type { HelloAck } from "./protocol";

export interface HubIdentity {
  /** `mid_<16hex>`, or null for a daemon from before TD-5015. */
  machineId: string | null;
  /** The daemon's computer name, for "<hub>" sections and copy. */
  desktopName: string | null;
}

export const hub = $state<HubIdentity>({ machineId: null, desktopName: null });

/** Record a handshake. Every ack replaces the last one, so reconnecting to
 *  an older daemon turns the machines feature back off. */
export function noteHandshake(ack: HelloAck): void {
  hub.machineId = typeof ack.machine_id === "string" ? ack.machine_id : null;
  hub.desktopName = typeof ack.desktop_name === "string" ? ack.desktop_name : null;
}

/** True when the daemon knows about other computers (sent machine_id). */
export function hubKnowsMachines(): boolean {
  return hub.machineId !== null;
}

/** Reset for tests. */
export function resetHub(): void {
  hub.machineId = null;
  hub.desktopName = null;
}
