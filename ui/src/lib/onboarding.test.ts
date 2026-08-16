// Tests for the onboarding store (TD-1101 first-run wizard).
//
// The store talks to the daemon only through connection-status's
// sendToDaemon/onEvent and session-status's openWorkspace, so the tests
// mock exactly those three seams and drive the same events the daemon emits.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => {
  const state = {
    handler: null as ((e: DaemonEventUnion) => void) | null,
    stateHandler: null as ((s: string) => void) | null,
    sent: [] as ClientMessageUnion[],
    opened: [] as string[],
    sendOk: true,
  };
  return state;
});

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
    mocks.sent.push(msg);
    return mocks.sendOk;
  },
}));

vi.mock("./session-status.svelte.js", () => ({
  openWorkspace: (path: string) => {
    mocks.opened.push(path);
  },
}));

import {
  onboarding,
  resetOnboarding,
  start,
  nextStep,
  backStep,
  reopen,
  closeWizard,
  storeKey,
  validateKey,
  removeKey,
  choosePreset,
  chooseWorkspace,
  finish,
} from "./onboarding.svelte.js";

function send(msg: ClientMessageUnion): ClientMessageUnion[] {
  return mocks.sent.filter((m) => m.type === msg.type);
}

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

const setupState = (hasKey: boolean, keyRequired = true): DaemonEventUnion =>
  ({
    type: "setup_state",
    seq: 1,
    has_api_key: hasKey,
    key_required: keyRequired,
    presets: ["budget", "local", "tst-default"],
    active_preset: "tst-default",
  }) as DaemonEventUnion;

beforeEach(() => {
  resetOnboarding();
  mocks.sent.length = 0;
  mocks.opened.length = 0;
  mocks.sendOk = true;
});

describe("first-run detection", () => {
  it("opens the wizard when the first setup_state reports no key", () => {
    start();
    emit(setupState(false));
    expect(onboarding.open).toBe(true);
    expect(onboarding.step).toBe("welcome");
    expect(onboarding.presets).toEqual(["budget", "local", "tst-default"]);
  });

  it("stays closed when a key already exists (returning user)", () => {
    start();
    emit(setupState(true));
    expect(onboarding.open).toBe(false);
    expect(onboarding.hasApiKey).toBe(true);
  });

  it("stays closed on a local-only preset that needs no key (TD-1801)", () => {
    start();
    emit(setupState(false, false));
    expect(onboarding.open).toBe(false);
    expect(onboarding.hasApiKey).toBe(false);
  });

  it("auto-opens at most once — closing is a decision, not a bug", () => {
    start();
    emit(setupState(false));
    closeWizard();
    emit(setupState(false));
    expect(onboarding.open).toBe(false);
  });

  it("probes setup state on every handshake-complete (connected)", () => {
    // The daemon never emits `ready`, so the client's own state transition
    // is the probe trigger; every connect and reconnect re-probes.
    start();
    mocks.stateHandler?.("connected");
    expect(mocks.sent.some((m) => m.type === "get_setup_state")).toBe(true);
    mocks.stateHandler?.("disconnected");
    mocks.stateHandler?.("connected");
    expect(mocks.sent.filter((m) => m.type === "get_setup_state").length).toBe(2);
  });
});

describe("key step", () => {
  it("storeKey sends the trimmed key and nothing else", () => {
    start();
    storeKey("  sk-test-abc  ");
    const msgs = send({ type: "set_api_key", api_key: "" });
    expect(msgs).toHaveLength(1);
    expect((msgs[0] as { api_key: string }).api_key).toBe("sk-test-abc");
  });

  it("ignores empty keys", () => {
    start();
    storeKey("   ");
    expect(mocks.sent.filter((m) => m.type === "set_api_key")).toHaveLength(0);
  });

  it("validate sends once, settles on api_key_validated", () => {
    start();
    emit(setupState(true)); // TD-1106: bare validate probes the stored key
    validateKey();
    expect(mocks.sent.some((m) => m.type === "validate_api_key")).toBe(true);
    expect(onboarding.validating).toBe(true);

    emit({ type: "api_key_validated", seq: 1, ok: true, detail: "ok" } as DaemonEventUnion);
    expect(onboarding.validating).toBe(false);
    expect(onboarding.validation?.ok).toBe(true);
    expect(onboarding.hasApiKey).toBe(true);
  });

  it("a failed validation is shown, not hidden", () => {
    start();
    validateKey("sk-bad"); // TD-1106: typed key — no stored state involved
    emit({
      type: "api_key_validated",
      seq: 1,
      ok: false,
      detail: "Authentication failed. Fix one of these...",
    } as DaemonEventUnion);
    expect(onboarding.validation?.ok).toBe(false);
    expect(onboarding.validation?.detail).toContain("Authentication failed");
    expect(onboarding.hasApiKey).toBe(false);
  });

  it("validating resets when the send never left (socket down)", () => {
    start();
    emit(setupState(true));
    mocks.sendOk = false;
    validateKey();
    expect(onboarding.validating).toBe(false);
  });

  it("validate with a typed key sends it, regardless of stored state (TD-1106)", () => {
    start(); // hasApiKey false — nothing stored
    validateKey("  sk-typed-1  ");
    const msgs = mocks.sent.filter((m) => m.type === "validate_api_key");
    expect(msgs).toHaveLength(1);
    expect((msgs[0] as { api_key?: string }).api_key).toBe("sk-typed-1");
    expect(onboarding.validating).toBe(true);
  });

  it("an ok verdict for a typed key does not flip hasApiKey (TD-1106)", () => {
    start();
    validateKey("sk-typed-1");
    emit({ type: "api_key_validated", seq: 1, ok: true, detail: "ok" } as DaemonEventUnion);
    expect(onboarding.validation?.ok).toBe(true);
    // The key was proven but never stored — setup_state stays the source
    // of truth for "has a key".
    expect(onboarding.hasApiKey).toBe(false);
  });

  it("bare validate with nothing stored is a no-op (TD-1106)", () => {
    start();
    validateKey();
    expect(mocks.sent.filter((m) => m.type === "validate_api_key")).toHaveLength(0);
    expect(onboarding.validating).toBe(false);
  });

  it("removeKey sends delete_api_key; the ack flips hasApiKey off (TD-1102)", () => {
    start();
    emit(setupState(true));
    expect(onboarding.hasApiKey).toBe(true);
    // A stale verdict about the old key must not survive its removal.
    emit({ type: "api_key_validated", seq: 2, ok: true, detail: "ok" } as DaemonEventUnion);
    expect(onboarding.validation).not.toBeNull();

    removeKey();
    expect(mocks.sent.some((m) => m.type === "delete_api_key")).toBe(true);
    expect(onboarding.validation).toBeNull();
    // hasApiKey flips only on the daemon's fresh setup_state, like storeKey.
    expect(onboarding.hasApiKey).toBe(true);
    emit(setupState(false));
    expect(onboarding.hasApiKey).toBe(false);
  });
});

describe("preset + workspace steps", () => {
  it("choosePreset sends the pick; the ack updates activePreset", () => {
    start();
    choosePreset("budget");
    expect(mocks.sent.some((m) => m.type === "set_preset" && (m as { name: string }).name === "budget")).toBe(true);
  });

  it("chooseWorkspace opens the workspace and advances to done", () => {
    start();
    chooseWorkspace("/Users/me/project");
    expect(mocks.opened).toEqual(["/Users/me/project"]);
    expect(onboarding.step).toBe("done");
  });
});

describe("step flow", () => {
  it("next/back walk the declared order and clamp at the ends", () => {
    start();
    expect(onboarding.step).toBe("welcome");
    nextStep();
    nextStep();
    expect(onboarding.step).toBe("preset");
    nextStep();
    expect(onboarding.step).toBe("workspace");
    nextStep();
    nextStep();
    expect(onboarding.step).toBe("done"); // clamped
    backStep();
    backStep();
    backStep();
    backStep();
    backStep();
    expect(onboarding.step).toBe("welcome"); // clamped
  });

  it("reopen restarts the wizard from welcome (settings path)", () => {
    start();
    emit(setupState(true));
    nextStep();
    nextStep();
    reopen();
    expect(onboarding.step).toBe("welcome");
    expect(onboarding.open).toBe(true);
  });

  it("finish closes the wizard", () => {
    start();
    emit(setupState(false));
    finish();
    expect(onboarding.open).toBe(false);
  });
});
