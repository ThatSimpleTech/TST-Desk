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
  openAddModel,
  setModelUrl,
  listModels,
  pickModel,
  addModel,
  testModel,
  backModelStep,
  isLocalUrl,
  labelFromSlug,
  scanLocalServers,
  chooseLocalServer,
} from "./onboarding.svelte.js";

function send(msg: ClientMessageUnion): ClientMessageUnion[] {
  return mocks.sent.filter((m) => m.type === msg.type);
}

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

const setupState = (hasKey: boolean, keyRequired = true, hasModel = true): DaemonEventUnion =>
  ({
    type: "setup_state",
    seq: 1,
    has_api_key: hasKey,
    key_required: keyRequired,
    presets: hasModel ? ["budget", "local", "tst-default"] : [],
    active_preset: hasModel ? "tst-default" : "",
    has_model: hasModel,
  }) as DaemonEventUnion;

const firstRun = (): DaemonEventUnion => setupState(false, false, false);

beforeEach(() => {
  resetOnboarding();
  mocks.sent.length = 0;
  mocks.opened.length = 0;
  mocks.sendOk = true;
});

describe("first-run detection", () => {
  it("opens the wizard when the first setup_state has no model (TD-5172)", () => {
    start();
    emit(firstRun());
    expect(onboarding.open).toBe(true);
    expect(onboarding.step).toBe("welcome");
    expect(onboarding.hasModel).toBe(false);
    expect(onboarding.presets).toEqual([]);
  });

  it("stays closed when a model is set up (returning user)", () => {
    start();
    emit(setupState(true));
    expect(onboarding.open).toBe(false);
    expect(onboarding.hasApiKey).toBe(true);
  });

  it("stays closed when a model exists but its key is missing: Settings → Keys fixes that", () => {
    start();
    emit(setupState(false));
    expect(onboarding.open).toBe(false);
    emit(setupState(false, false));
    expect(onboarding.open).toBe(false);
  });

  it("auto-opens at most once — closing is a decision, not a bug", () => {
    start();
    emit(firstRun());
    closeWizard();
    emit(firstRun());
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
    expect(onboarding.step).toBe("model");
    nextStep();
    expect(onboarding.step).toBe("workspace");
    nextStep();
    nextStep();
    expect(onboarding.step).toBe("done"); // clamped
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
    emit(firstRun());
    finish();
    expect(onboarding.open).toBe(false);
  });
});

describe("Add a model (TD-5172)", () => {
  const listed = (error: string | null = null): DaemonEventUnion =>
    ({
      type: "endpoint_models",
      seq: 1,
      base_url: "https://api.example.com/v1",
      models: error
        ? []
        : [
            { id: "vendor/model-a", context_window: 128000, input_price: 3, output_price: 15 },
            { id: "vendor/model-b" },
          ],
      error,
    }) as DaemonEventUnion;

  it("opens straight on the step from the picker or Settings", () => {
    openAddModel();
    expect(onboarding.open).toBe(true);
    expect(onboarding.step).toBe("model");
    expect(onboarding.model.sub).toBe("url");
    expect(onboarding.addOnly).toBe(true);
  });

  it("a hosted URL asks for a key, then lists models with it", () => {
    start();
    openAddModel();
    setModelUrl("  https://api.example.com/v1  ");
    expect(onboarding.model.sub).toBe("key");
    expect(send({ type: "list_endpoint_models" } as ClientMessageUnion)).toHaveLength(0);
    listModels(" sk-test ");
    expect(send({ type: "list_endpoint_models" } as ClientMessageUnion)).toEqual([
      { type: "list_endpoint_models", base_url: "https://api.example.com/v1", api_key: "sk-test" },
    ]);
    expect(onboarding.model.listing).toBe(true);
    emit(listed());
    expect(onboarding.model.listing).toBe(false);
    expect(onboarding.model.sub).toBe("pick");
    expect(onboarding.model.models.map((m) => m.id)).toEqual(["vendor/model-a", "vendor/model-b"]);
  });

  it("a server on this computer skips the key", () => {
    start();
    openAddModel();
    setModelUrl("http://127.0.0.1:8000/v1");
    const sent = send({ type: "list_endpoint_models" } as ClientMessageUnion);
    expect(sent).toEqual([{ type: "list_endpoint_models", base_url: "http://127.0.0.1:8000/v1" }]);
    emit(listed());
    expect(onboarding.model.sub).toBe("pick");
    backModelStep();
    expect(onboarding.model.sub).toBe("url");
  });

  it("a listing error stays on the step and says why", () => {
    start();
    openAddModel();
    setModelUrl("https://api.example.com/v1");
    listModels("sk-bad");
    emit(listed("The server refused the key. Check it and try again."));
    expect(onboarding.model.sub).toBe("key");
    expect(onboarding.model.listError).toContain("refused the key");
  });

  it("pick → label → save sends add_model, then tests the new model", () => {
    start();
    openAddModel();
    setModelUrl("https://api.example.com/v1");
    listModels("sk-test");
    emit(listed());
    pickModel(onboarding.model.models[0]);
    expect(onboarding.model.sub).toBe("label");
    expect(onboarding.model.label).toBe("model-a");
    addModel(" My model ", " Coding and chat ");
    expect(send({ type: "add_model" } as ClientMessageUnion)).toEqual([
      {
        type: "add_model",
        label: "My model",
        description: "Coding and chat",
        base_url: "https://api.example.com/v1",
        slug: "vendor/model-a",
        api_key: "sk-test",
        context_window: 128000,
        input_price: 3,
        output_price: 15,
      },
    ]);
    emit({ ...setupState(true), active_preset: "my-model" } as DaemonEventUnion);
    expect(onboarding.model.sub).toBe("test");
    expect(onboarding.model.added).toBe("my-model");
    expect(onboarding.model.apiKey).toBe(""); // dropped once the daemon has it
    expect(send({ type: "test_model" } as ClientMessageUnion)).toEqual([
      { type: "test_model", preset: "my-model" },
    ]);
    emit({
      type: "model_tested",
      seq: 1,
      preset: "my-model",
      ok: true,
      reply: "I am working.",
      detail: "",
    } as DaemonEventUnion);
    expect(onboarding.model.testing).toBe(false);
    expect(onboarding.model.test).toEqual({ ok: true, reply: "I am working.", detail: "" });
  });

  it("a refused save shows the daemon's message", () => {
    start();
    openAddModel();
    setModelUrl("http://127.0.0.1:8000/v1");
    emit(listed());
    pickModel({ id: "local-model" });
    addModel("Local", "");
    emit({ type: "error", seq: 1, code: "bad_request", message: "That name is taken" } as DaemonEventUnion);
    expect(onboarding.model.adding).toBe(false);
    expect(onboarding.model.addError).toBe("That name is taken");
    expect(onboarding.model.sub).toBe("label");
  });

  it("closing drops a typed key", () => {
    start();
    openAddModel();
    setModelUrl("https://api.example.com/v1");
    listModels("sk-test");
    closeWizard();
    expect(onboarding.model.apiKey).toBe("");
  });

  it("test again re-sends", () => {
    start();
    testModel();
    expect(send({ type: "test_model" } as ClientMessageUnion)).toEqual([{ type: "test_model" }]);
  });

  it("helpers: local URLs and labels from ids", () => {
    expect(isLocalUrl("http://127.0.0.1:8000/v1")).toBe(true);
    expect(isLocalUrl("http://localhost:11434/v1")).toBe(true);
    expect(isLocalUrl("http://[::1]:8000/v1")).toBe(true);
    expect(isLocalUrl("https://api.example.com/v1")).toBe(false);
    expect(isLocalUrl("not a url")).toBe(false);
    expect(labelFromSlug("vendor/model-a")).toBe("model-a");
    expect(labelFromSlug("plain")).toBe("plain");
  });
});

describe("Find models on this computer (TD-5180)", () => {
  const ollama = {
    name: "Ollama",
    base_url: "http://127.0.0.1:11434/v1",
    models: [{ id: "llama3.2:3b" }, { id: "qwen3:8b" }],
  };
  const answered = (servers = [ollama], checked = [ollama.base_url]): DaemonEventUnion =>
    ({ type: "local_servers", seq: 1, servers, checked }) as DaemonEventUnion;

  beforeEach(() => {
    start();
    openAddModel();
  });

  it("asks the daemon only when clicked, once while a scan is in flight", () => {
    expect(send({ type: "scan_local_servers" } as ClientMessageUnion)).toHaveLength(0);
    scanLocalServers();
    scanLocalServers();
    expect(send({ type: "scan_local_servers" } as ClientMessageUnion)).toEqual([
      { type: "scan_local_servers" },
    ]);
    expect(onboarding.model.scanning).toBe(true);
  });

  it("keeps what answered and where it looked", () => {
    scanLocalServers();
    emit(answered([ollama], [ollama.base_url, "http://127.0.0.1:1234/v1"]));
    expect(onboarding.model.scanning).toBe(false);
    expect(onboarding.model.scan?.servers.map((s) => s.name)).toEqual(["Ollama"]);
    expect(onboarding.model.scan?.checked).toHaveLength(2);
  });

  it("ignores an answer nobody asked for", () => {
    emit(answered());
    expect(onboarding.model.scan).toBeNull();
  });

  it("a found server goes straight to the pick, with no key and no second request", () => {
    scanLocalServers();
    emit(answered());
    chooseLocalServer(ollama);
    expect(onboarding.model.sub).toBe("pick");
    expect(onboarding.model.baseUrl).toBe(ollama.base_url);
    expect(onboarding.model.apiKey).toBe("");
    expect(onboarding.model.models.map((m) => m.id)).toEqual(["llama3.2:3b", "qwen3:8b"]);
    expect(send({ type: "list_endpoint_models" } as ClientMessageUnion)).toHaveLength(0);
    backModelStep();
    expect(onboarding.model.sub).toBe("url");
  });

  it("says so when TST Desk is not connected", () => {
    mocks.sendOk = false;
    scanLocalServers();
    expect(onboarding.model.scanning).toBe(false);
    expect(onboarding.model.listError).toMatch(/not connected/);
  });

  it("a refused scan stops looking and says why", () => {
    scanLocalServers();
    emit({
      type: "error",
      seq: 1,
      code: "loopback_only",
      message: "Only the computer itself can do that.",
      in_reply_to: "scan_local_servers",
    } as DaemonEventUnion);
    expect(onboarding.model.scanning).toBe(false);
    expect(onboarding.model.listError).toBe("Only the computer itself can do that.");
  });

  it("a fresh Add a model starts with no scan result", () => {
    scanLocalServers();
    emit(answered());
    openAddModel();
    expect(onboarding.model.scan).toBeNull();
    expect(onboarding.model.scanning).toBe(false);
  });
});
