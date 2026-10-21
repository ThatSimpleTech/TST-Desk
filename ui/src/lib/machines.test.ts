// Other computers (TD-5015): the pure copy and validation in machines.ts,
// and the machines.svelte.ts store with the connection mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, HelloAck, MachineSummary } from "./protocol";

const mocks = vi.hoisted(() => ({
  handler: null as ((e: DaemonEventUnion) => void) | null,
  stateHandler: null as ((s: string) => void) | null,
  sent: [] as ClientMessageUnion[],
  ws: { state: "idle" as string },
  sendOk: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (e: DaemonEventUnion) => void) => {
    mocks.handler = handler;
    return () => {
      mocks.handler = null;
    };
  },
  onConnectionState: (handler: (s: string) => void) => {
    mocks.stateHandler = handler;
    return () => {
      mocks.stateHandler = null;
    };
  },
  sendToDaemon: (msg: ClientMessageUnion) => {
    if (!mocks.sendOk) return false;
    mocks.sent.push(msg);
    return true;
  },
  ws: mocks.ws,
}));

import {
  ADD_MACHINE_ERRORS,
  STATUS_WORDS,
  addMachineError,
  alsoSeesCopy,
  alsoSeesFor,
  checkAddMachine,
  firstSessionCopy,
  isOffline,
  knownWorkspaces,
  lastSeenWords,
  readOnlyCopy,
  removeConfirmCopy,
  staysOnCopy,
  statusLine,
  statusTone,
  unreachableNotice,
} from "./machines";
import { noteHandshake, resetHub } from "./hub.svelte.js";
import { claimedInline } from "./inline-errors";
import {
  ADD_TIMEOUT_MS,
  addMachine,
  askRemoveMachine,
  machineById,
  machineForSession,
  machineNameFor,
  machines,
  nameOfMachine,
  reconnectMachine,
  refreshMachines,
  removeMachine,
  resetMachines,
  sessionMachineUp,
  startMachines,
} from "./machines.svelte.js";

const NOW = Date.parse("2026-10-14T12:00:00Z");
const MCH_A = "mch_aaaaaaaaaaaa";
const MCH_B = "mch_bbbbbbbbbbbb";
const RAW = "3f2a9c1e-7b4d-4e8a-9f01-2c3d4e5f6a7b";

function machine(over: Partial<MachineSummary> = {}): MachineSummary {
  return {
    id: MCH_A,
    name: "TOWER",
    host: "100.64.0.2",
    port: 39079,
    status: "connected",
    last_seen: "2026-10-14T10:00:00Z",
    paired_at: "2026-10-01T09:00:00Z",
    same_computer: false,
    detail: null,
    ...over,
  };
}

function ack(over: Partial<HelloAck> = {}): HelloAck {
  return {
    type: "hello_ack",
    seq: 1,
    protocol_version: 1,
    machine_id: "mid_0123456789abcdef",
    desktop_name: "studio",
    ...over,
  } as HelloAck;
}

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

function list(...ms: MachineSummary[]): DaemonEventUnion {
  return { type: "machine_list", seq: 2, machines: ms } as DaemonEventUnion;
}

describe("status words and lines", () => {
  it("has a word and tone for every status", () => {
    expect(STATUS_WORDS.unreachable).toBe("Not reachable");
    expect(statusTone("connected")).toBe("ok");
    expect(statusTone("connecting")).toBe("info");
    expect(statusTone("unreachable")).toBe("muted");
    expect(statusTone("refused")).toBe("warn");
    expect(statusTone("locked")).toBe("warn");
    expect(statusTone("outdated")).toBe("warn");
  });

  it("says nothing under a connected computer", () => {
    expect(statusLine(machine(), NOW)).toBeNull();
  });

  it("reads 'Not reachable · last seen 2 hours ago'", () => {
    expect(statusLine(machine({ status: "unreachable" }), NOW)).toBe("Not reachable · last seen 2 hours ago");
    expect(statusLine(machine({ status: "unreachable", last_seen: null }), NOW)).toBe("Not reachable");
    expect(lastSeenWords(machine({ last_seen: null }), NOW)).toBeNull();
  });

  it("explains a refused, locked or outdated link", () => {
    expect(statusLine(machine({ status: "refused" }), NOW)).toBe(
      "TOWER no longer knows this computer. Pair again or remove it.",
    );
    expect(statusLine(machine({ status: "locked" }), NOW)).toContain("keychain is locked");
    expect(statusLine(machine({ status: "outdated" }), NOW)).toContain("needs a newer TST Desk");
  });

  it("greys a computer's rows only while it is not connected", () => {
    expect(isOffline(null)).toBe(false);
    expect(isOffline(machine())).toBe(false);
    expect(isOffline(machine({ status: "connecting" }))).toBe(true);
    expect(isOffline(machine({ status: "unreachable" }))).toBe(true);
  });
});

describe("copy", () => {
  it("warns that an offline computer keeps listing this one", () => {
    expect(removeConfirmCopy(machine())).toBe("Remove TOWER? Its sessions leave this list.");
    expect(removeConfirmCopy(machine({ status: "unreachable" }))).toBe(
      "Remove TOWER? Its sessions leave this list. TOWER still lists this computer until it's removed there.",
    );
  });

  it("uses the spec's sentences", () => {
    expect(unreachableNotice("TOWER")).toBe("TOWER isn't reachable. This session opens when it is.");
    expect(firstSessionCopy("TOWER")).toBe("Start the first session on TOWER; it shows up here.");
    expect(staysOnCopy("TOWER")).toBe("These stay on TOWER.");
    expect(readOnlyCopy("studio")).toBe("Add or remove computers in Settings on studio.");
  });

  it("names the computers a paired phone also sees, leaving out a self-pair", () => {
    expect(alsoSeesCopy([])).toBeNull();
    expect(alsoSeesCopy(["TOWER", "mini"])).toBe("This phone will also see sessions on: TOWER, mini.");
    expect(alsoSeesFor([machine(), machine({ id: MCH_B, name: "studio", same_computer: true })])).toBe(
      "This phone will also see sessions on: TOWER.",
    );
    expect(alsoSeesFor([machine({ same_computer: true })])).toBeNull();
  });
});

describe("knownWorkspaces", () => {
  it("lists each workspace once, newest first, with its newest session as the anchor", () => {
    const rows = [
      { sessionId: `a:${MCH_A}`, workspacePath: "/w/api", updatedAt: "2026-10-14T09:00:00Z" },
      { sessionId: `b:${MCH_A}`, workspacePath: "/w/web", updatedAt: "2026-10-14T11:00:00Z" },
      { sessionId: `c:${MCH_A}`, workspacePath: "/w/api", updatedAt: "2026-10-14T10:00:00Z" },
    ];
    expect(knownWorkspaces(rows)).toEqual([
      { path: "/w/web", anchor: `b:${MCH_A}` },
      { path: "/w/api", anchor: `c:${MCH_A}` },
    ]);
    expect(knownWorkspaces([])).toEqual([]);
  });
});

describe("checkAddMachine", () => {
  it("sends a pasted link as is", () => {
    const link = "tstdesk://pair?h=100.64.0.2&p=39079&c=ABCD2345&n=TOWER";
    expect(checkAddMachine({ link: `  ${link} ` })).toEqual({ ok: true, message: { type: "add_machine", link } });
  });

  it("sends a typed address and a normalized code", () => {
    expect(checkAddMachine({ address: "100.64.0.2:39079", code: "abcd-2345" })).toEqual({
      ok: true,
      message: { type: "add_machine", host: "100.64.0.2", port: 39079, code: "ABCD2345" },
    });
  });

  it("refuses an empty link, a non-Tailscale host and a bad code before sending", () => {
    expect(checkAddMachine({ link: "" }).ok).toBe(false);
    const lan = checkAddMachine({ link: "tstdesk://pair?h=192.168.1.5&p=39079&c=ABCD2345" });
    expect(lan).toEqual({ ok: false, error: "That address isn't on Tailscale, so this app won't connect to it." });
    expect(checkAddMachine({ address: "100.64.0.2:39079", code: "nope" }).ok).toBe(false);
    expect(checkAddMachine({ address: "", code: "ABCD2345" }).ok).toBe(false);
  });

  it("maps every inline add error to plain words, and passes others through", () => {
    for (const code of ["pair_failed", "pair_locked", "machine_unreachable", "not_tailscale", "machine_limit", "keychain_locked"]) {
      expect(ADD_MACHINE_ERRORS.has(code), code).toBe(true);
      expect(addMachineError(code, "raw")).not.toBe("raw");
    }
    expect(addMachineError("something_else", "raw message")).toBe("raw message");
  });
});

describe("machines store", () => {
  let off: () => void = () => {};

  beforeEach(() => {
    vi.useFakeTimers();
    mocks.sent = [];
    mocks.sendOk = true;
    mocks.ws.state = "idle";
    resetHub();
    resetMachines();
    off = startMachines();
  });

  afterEach(() => {
    off();
    vi.useRealTimers();
  });

  it("never sends list_machines to a daemon from before TD-5015", () => {
    noteHandshake(ack({ machine_id: undefined, desktop_name: undefined }));
    mocks.stateHandler?.("connected");
    expect(refreshMachines()).toBe(false);
    expect(mocks.sent).toEqual([]);
    expect(machines.list).toEqual([]);
  });

  it("asks for the list on connect when the hub knows machines", () => {
    noteHandshake(ack());
    mocks.stateHandler?.("connected");
    expect(mocks.sent).toEqual([{ type: "list_machines" }]);
    emit(list(machine()));
    expect(machines.list.map((m) => m.id)).toEqual([MCH_A]);
    expect(machines.loaded).toBe(true);
  });

  it("drops the list when the window reaches another hub or an old daemon", () => {
    noteHandshake(ack());
    mocks.stateHandler?.("connected");
    emit(list(machine()));
    noteHandshake(ack({ machine_id: undefined }));
    mocks.stateHandler?.("connected");
    expect(machines.list).toEqual([]);
  });

  it("keeps the list across a reconnect to the same hub", () => {
    noteHandshake(ack());
    mocks.stateHandler?.("connected");
    emit(list(machine()));
    mocks.stateHandler?.("connected");
    expect(machines.list).toHaveLength(1);
  });

  it("answers which computer a session is on", () => {
    emit(list(machine(), machine({ id: MCH_B, name: "mini", status: "unreachable" })));
    expect(machineById(MCH_B)?.name).toBe("mini");
    expect(machineById(null)).toBeNull();
    expect(machineForSession(RAW)).toBeNull();
    expect(machineForSession(`${RAW}:${MCH_B}`)?.id).toBe(MCH_B);
    expect(sessionMachineUp(RAW)).toBe(true);
    expect(sessionMachineUp(`${RAW}:${MCH_A}`)).toBe(true);
    expect(sessionMachineUp(`${RAW}:${MCH_B}`)).toBe(false);
    expect(nameOfMachine(MCH_A)).toBe("TOWER");
    expect(machineNameFor(RAW)).toBeNull();
    expect(machineNameFor(`${RAW}:${MCH_A}`)).toBe("TOWER");
    expect(machineNameFor(`${RAW}:mch_cccccccccccc`)).toBe("another computer");
  });

  it("validates before sending an add, and shows the error inline", () => {
    expect(addMachine({ link: "https://example.com" })).toBe(false);
    expect(mocks.sent).toEqual([]);
    expect(machines.addError).not.toBeNull();
  });

  it("reports the computer an add paired", () => {
    emit(list(machine()));
    expect(addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" })).toBe(true);
    expect(machines.adding).toBe(true);
    expect(addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" })).toBe(false);
    emit(list(machine(), machine({ id: MCH_B, name: "mini", paired_at: "2026-10-14T11:59:00Z" })));
    expect(machines.adding).toBe(false);
    expect(machines.added).toBe("mini");
    expect(machines.addError).toBeNull();
  });

  it("maps an add error to its inline copy, and ignores errors that name a session", () => {
    addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" });
    emit({ type: "error", seq: 3, code: "pair_failed", message: "x", session_id: RAW } as DaemonEventUnion);
    expect(machines.adding).toBe(true);
    emit({ type: "error", seq: 4, code: "pair_failed", message: "x" } as DaemonEventUnion);
    expect(machines.adding).toBe(false);
    expect(machines.addError).toBe(addMachineError("pair_failed", "x"));
  });

  it("ignores another computer's relayed error, and claims its own so no global notice repeats it", () => {
    addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" });
    const relayed = {
      type: "error",
      seq: 3,
      code: "keychain_locked",
      message: "On tower: locked",
      machine: "mch_0123456789ab",
    } as DaemonEventUnion;
    emit(relayed);
    expect(machines.adding).toBe(true);
    expect(claimedInline(relayed)).toBe(false);
    const own = { type: "error", seq: 4, code: "keychain_locked", message: "locked" } as DaemonEventUnion;
    emit(own);
    expect(machines.adding).toBe(false);
    expect(machines.addError).toBe(addMachineError("keychain_locked", "locked"));
    expect(claimedInline(own)).toBe(true);
  });

  it("gives up on an add that never answers", () => {
    addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" });
    vi.advanceTimersByTime(ADD_TIMEOUT_MS + 1);
    expect(machines.adding).toBe(false);
    expect(machines.addError).toBe(addMachineError("machine_unreachable", ""));
  });

  it("says so when the socket is down", () => {
    mocks.sendOk = false;
    expect(addMachine({ address: "100.64.0.3:39079", code: "ABCD2345" })).toBe(false);
    expect(machines.adding).toBe(false);
    expect(machines.addError).toContain("isn't connected");
  });

  it("removes and reconnects by handle, and clears a stale confirm", () => {
    emit(list(machine()));
    askRemoveMachine(MCH_A);
    expect(machines.confirmRemoveFor).toBe(MCH_A);
    expect(removeMachine(MCH_A)).toBe(true);
    expect(machines.confirmRemoveFor).toBeNull();
    expect(reconnectMachine(MCH_A)).toBe(true);
    expect(mocks.sent).toEqual([
      { type: "remove_machine", machine: MCH_A },
      { type: "reconnect_machine", machine: MCH_A },
    ]);
    askRemoveMachine(MCH_A);
    emit(list());
    expect(machines.confirmRemoveFor).toBeNull();
  });

  it("refreshes the list after machine_not_found", () => {
    noteHandshake(ack());
    emit({ type: "error", seq: 5, code: "machine_not_found", message: "gone" } as DaemonEventUnion);
    expect(mocks.sent).toEqual([{ type: "list_machines" }]);
  });
});
