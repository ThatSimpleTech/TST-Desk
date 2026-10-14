// Timers behind a phone's saved connection (TD-5014).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoreRetry, UnreachableWatch, type VisibilityDoc } from "./remote-watch";

function fakeDoc() {
  const listeners = new Set<() => void>();
  const doc = {
    visibilityState: "hidden",
    addEventListener: (_type: "visibilitychange", fn: () => void) => void listeners.add(fn),
    removeEventListener: (_type: "visibilitychange", fn: () => void) => void listeners.delete(fn),
  };
  return {
    doc: doc as VisibilityDoc,
    listeners,
    show() {
      doc.visibilityState = "visible";
      for (const fn of [...listeners]) fn();
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("StoreRetry (TD-5014)", () => {
  it("looks again at about 1 s and 5 s, then settles", () => {
    const attempt = vi.fn(() => false);
    const settle = vi.fn();
    new StoreRetry(attempt, settle).start(null);
    vi.advanceTimersByTime(999);
    expect(attempt).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(settle).not.toHaveBeenCalled();
    vi.advanceTimersByTime(4_000);
    expect(attempt).toHaveBeenCalledTimes(2);
    expect(settle).toHaveBeenCalledTimes(1);
  });

  it("stops as soon as a try works", () => {
    const attempt = vi.fn(() => true);
    const settle = vi.fn();
    const page = fakeDoc();
    new StoreRetry(attempt, settle).start(page.doc);
    vi.advanceTimersByTime(10_000);
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(settle).not.toHaveBeenCalled();
    expect(page.listeners.size).toBe(0);
  });

  it("tries again whenever the app comes back to the front, after settling too", () => {
    let works = false;
    const attempt = vi.fn(() => works);
    const page = fakeDoc();
    new StoreRetry(attempt, () => {}).start(page.doc);
    vi.advanceTimersByTime(5_000);
    expect(attempt).toHaveBeenCalledTimes(2);
    works = true;
    page.show();
    expect(attempt).toHaveBeenCalledTimes(3);
    expect(page.listeners.size).toBe(0);
  });

  it("retries on demand and can be stopped", () => {
    const attempt = vi.fn(() => false);
    const retry = new StoreRetry(attempt, () => {});
    retry.start(null);
    expect(retry.retryNow()).toBe(false);
    retry.stop();
    vi.advanceTimersByTime(10_000);
    expect(attempt).toHaveBeenCalledTimes(1);
  });
});

describe("UnreachableWatch (TD-5014)", () => {
  it("flags a saved computer that has not answered for the whole wait", () => {
    const onChange = vi.fn();
    const watch = new UnreachableWatch(60_000, onChange);
    watch.update("connecting", true);
    vi.advanceTimersByTime(30_000);
    watch.update("reconnecting", true);
    vi.advanceTimersByTime(29_999);
    expect(onChange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onChange).toHaveBeenLastCalledWith(true);
    watch.update("connected", true);
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("starts over after a connection, and ignores a device with nothing saved", () => {
    const onChange = vi.fn();
    const watch = new UnreachableWatch(60_000, onChange);
    watch.update("reconnecting", true);
    vi.advanceTimersByTime(50_000);
    watch.update("connected", true);
    watch.update("reconnecting", true);
    vi.advanceTimersByTime(50_000);
    expect(onChange).not.toHaveBeenCalled();
    watch.update("reconnecting", false);
    vi.advanceTimersByTime(60_000);
    expect(onChange).not.toHaveBeenCalled();
  });
});
