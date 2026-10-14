import { describe, expect, it } from "vitest";
import { attachStore, nativeAttachBridge, REMOTE_ATTACH_STORAGE } from "./attach-store";

function fakeBridge() {
  let saved: string | null = null;
  return {
    calls: [] as string[],
    loadAttach() {
      this.calls.push("load");
      return saved;
    },
    saveAttach(raw: string) {
      this.calls.push("save");
      saved = raw;
      return true;
    },
    clearAttach() {
      this.calls.push("clear");
      saved = null;
    },
  };
}

function fakeSession(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k) => map.get(k) ?? null,
    key: (i) => [...map.keys()][i] ?? null,
    removeItem: (k) => void map.delete(k),
    setItem: (k, v) => void map.set(k, v),
  };
}

describe("attachStore (TD-5005)", () => {
  it("uses the Android bridge when the shell injects it, and survives a relaunch", () => {
    const bridge = fakeBridge();
    const scope = { TstDeskNative: bridge, sessionStorage: fakeSession() };
    const store = attachStore(scope);
    expect(store.durable).toBe(true);
    store.save('{"host":"100.64.7.7","port":39079,"token":"t"}');
    // A fresh store over the same bridge is the next launch.
    expect(attachStore(scope).load()).toContain("39079");
    expect(scope.sessionStorage.getItem(REMOTE_ATTACH_STORAGE)).toBeNull();
    store.clear();
    expect(attachStore(scope).load()).toBeNull();
  });

  it("keeps the browser-tab rule when there is no bridge", () => {
    const session = fakeSession();
    const store = attachStore({ sessionStorage: session });
    expect(store.durable).toBe(false);
    store.save("raw");
    expect(session.getItem(REMOTE_ATTACH_STORAGE)).toBe("raw");
    store.clear();
    expect(store.load()).toBeNull();
  });

  it("accepts a 0.1.5 shell without the pairing methods, and a newer one with them (TD-5014)", () => {
    const old = fakeBridge();
    expect(nativeAttachBridge({ TstDeskNative: old })).toBe(old);
    const newer = { ...fakeBridge(), deviceName: () => "Pixel", scanPairing: () => true, takePairLink: () => null, attachStatus: () => "ok" };
    const bridge = nativeAttachBridge({ TstDeskNative: newer });
    expect(bridge?.deviceName?.()).toBe("Pixel");
    expect(attachStore({ TstDeskNative: newer }).durable).toBe(true);
  });

  it("ignores a partial bridge from an older shell", () => {
    expect(nativeAttachBridge({ TstDeskNative: { loadAttach: () => null } })).toBeNull();
    expect(attachStore({ TstDeskNative: { loadAttach: () => null } }).durable).toBe(false);
  });

  it("treats an empty native answer as nothing saved", () => {
    const bridge = { loadAttach: () => "", saveAttach: () => true, clearAttach: () => {} };
    expect(attachStore({ TstDeskNative: bridge }).load()).toBeNull();
  });

  it("survives a bridge that throws", () => {
    const bridge = {
      loadAttach: () => {
        throw new Error("keystore");
      },
      saveAttach: () => {
        throw new Error("keystore");
      },
      clearAttach: () => {
        throw new Error("keystore");
      },
    };
    const store = attachStore({ TstDeskNative: bridge });
    expect(store.load()).toBeNull();
    expect(store.save("x")).toBe(false);
    expect(() => store.clear()).not.toThrow();
  });

  it("is a no-op with neither bridge nor sessionStorage", () => {
    const store = attachStore({});
    expect(store.save("x")).toBe(false);
    expect(store.load()).toBeNull();
  });

  it("says whether the save took (TD-5014)", () => {
    expect(attachStore({ TstDeskNative: fakeBridge() }).save("raw")).toBe(true);
    const refusing = { ...fakeBridge(), saveAttach: () => false };
    expect(attachStore({ TstDeskNative: refusing }).save("raw")).toBe(false);
    expect(attachStore({ sessionStorage: fakeSession() }).save("raw")).toBe(true);
    const full = {
      ...fakeSession(),
      setItem: () => {
        throw new Error("quota");
      },
    };
    expect(attachStore({ sessionStorage: full }).save("raw")).toBe(false);
  });
});
