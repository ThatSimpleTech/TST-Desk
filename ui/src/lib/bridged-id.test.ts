// Bridged session ids (TD-5015): the grammar, and the verbs the UI may send
// for a session on another computer.
import { describe, expect, it } from "vitest";
import fixtures from "./protocol-fixtures.json";
import { BRIDGED_ID_RE, BRIDGED_VERBS, isBridged, machineOf, rawId, refusedForBridged } from "./bridged-id";
import type { ClientMessageUnion } from "./protocol";

const OWN = "3f2a9c1e-7b4d-4e8a-9f01-2c3d4e5f6a7b";
const MCH = "mch_0123456789ab";
const BRIDGED = `${OWN}:${MCH}`;

describe("bridged id grammar", () => {
  it("names the machine of a bridged id", () => {
    expect(machineOf(BRIDGED)).toBe(MCH);
    expect(isBridged(BRIDGED)).toBe(true);
    expect(rawId(BRIDGED)).toBe(OWN);
  });

  it("leaves this computer's own ids alone", () => {
    expect(machineOf(OWN)).toBeNull();
    expect(isBridged(OWN)).toBe(false);
    expect(rawId(OWN)).toBe(OWN);
  });

  it("rejects anything that is not exactly <raw>:mch_<12 hex>", () => {
    for (const id of [
      `${OWN}:mch_0123456789`, // too short
      `${OWN}:mch_0123456789abc`, // too long
      `${OWN}:mch_0123456789AB`, // upper-case hex
      `${OWN}:mid_0123456789ab`, // a hub id, not a machine handle
      `a:b:${MCH}`, // two hops never reach a viewer
      `:${MCH}`, // empty raw part
      `${BRIDGED} `,
    ]) {
      expect(machineOf(id), id).toBeNull();
    }
    expect(machineOf(null)).toBeNull();
    expect(machineOf(undefined)).toBeNull();
  });

  it("exposes the named groups", () => {
    const match = BRIDGED_ID_RE.exec(BRIDGED);
    expect(match?.groups).toEqual({ raw: OWN, mch: MCH });
  });
});

describe("refusedForBridged", () => {
  it("lets forwarded verbs through for a bridged session", () => {
    const msg = { type: "user_message", session_id: BRIDGED, text: "hi" } as unknown as ClientMessageUnion;
    expect(refusedForBridged(msg)).toBe(false);
  });

  it("drops a local-only verb that names a bridged session", () => {
    for (const type of ["read_text_file", "list_artifacts", "set_design_mode", "open_in_terminal"]) {
      const msg = { type, session_id: BRIDGED } as unknown as ClientMessageUnion;
      expect(refusedForBridged(msg), type).toBe(true);
    }
  });

  it("never refuses a message about this computer's own session, or none", () => {
    expect(refusedForBridged({ type: "read_text_file", session_id: OWN } as unknown as ClientMessageUnion)).toBe(false);
    expect(refusedForBridged({ type: "list_machines" } as unknown as ClientMessageUnion)).toBe(false);
  });
});

describe("BRIDGED_VERBS", () => {
  it("includes every turn and approval verb a remote session needs", () => {
    for (const verb of ["attach", "detach", "user_message", "approve", "deny", "always_allow", "new_session"]) {
      expect(BRIDGED_VERBS.has(verb), verb).toBe(true);
    }
  });

  it("never includes a machine verb", () => {
    for (const verb of ["add_machine", "remove_machine", "list_machines", "reconnect_machine"]) {
      expect(BRIDGED_VERBS.has(verb), verb).toBe(false);
    }
  });

  // Pinned against core's FORWARDED_VERBS through the shared fixtures. This
  // fails until core's regenerated protocol-fixtures.json (with its
  // `forwarded_verbs` key) is merged; the UI must not edit that file.
  it("matches core's forwarded_verbs fixture", () => {
    const pinned = (fixtures as Record<string, unknown>).forwarded_verbs;
    expect(Array.isArray(pinned)).toBe(true);
    expect([...(pinned as string[])].sort()).toEqual([...BRIDGED_VERBS].sort());
  });
});
