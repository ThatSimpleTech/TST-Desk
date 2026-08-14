// Auto-scroll behavior tests (TD-1004). Node environment with a fake
// container: the logic under test is threshold math and pin/follow/jump
// transitions, which are DOM-free by design.

import { describe, it, expect } from "vitest";
import {
  BOTTOM_THRESHOLD_PX,
  createAutoScroll,
  isAtBottom,
  type ScrollContainerLike,
} from "./autoscroll";

interface FakeContainer extends ScrollContainerLike {
  fireScroll(): void;
  listenerCount(): number;
  setScrollHeight(value: number): void;
}

function fakeContainer(overrides: Partial<ScrollContainerLike> = {}): FakeContainer {
  let listener: (() => void) | null = null;
  // scrollHeight is readonly on ScrollContainerLike (matching the DOM), so
  // the fake backs it with a mutable field + getter and grows it via setter.
  let contentHeight = 1000;
  return {
    scrollTop: 0,
    clientHeight: 500,
    get scrollHeight() {
      return contentHeight;
    },
    setScrollHeight(value: number) {
      contentHeight = value;
    },
    addEventListener(_type, fn) {
      listener = fn;
    },
    removeEventListener(_type, fn) {
      if (listener === fn) listener = null;
    },
    fireScroll() {
      listener?.();
    },
    listenerCount() {
      return listener === null ? 0 : 1;
    },
    ...overrides,
  };
}

describe("isAtBottom", () => {
  it("is true at and near the bottom, false further up", () => {
    expect(isAtBottom(500, 500, 1000)).toBe(true); // exactly bottom
    expect(isAtBottom(500 + 500 - BOTTOM_THRESHOLD_PX, 500, 1000)).toBe(true); // threshold edge
    expect(isAtBottom(400, 500, 1000)).toBe(false); // 100px up
  });

  it("honors a custom threshold", () => {
    expect(isAtBottom(400, 500, 1000, 120)).toBe(true);
  });
});

describe("createAutoScroll", () => {
  it("follows new content while pinned", () => {
    const c = fakeContainer({ scrollTop: 500 });
    const auto = createAutoScroll(c);
    c.fireScroll(); // user sits at the bottom
    expect(auto.pinned).toBe(true);
    c.setScrollHeight(1200); // new content arrives
    auto.maybeFollow();
    expect(c.scrollTop).toBe(1200);
  });

  it("stops following once the user scrolls up", () => {
    const c = fakeContainer({ scrollTop: 500 });
    const auto = createAutoScroll(c);
    c.fireScroll();
    expect(auto.pinned).toBe(true);
    c.scrollTop = 200; // user scrolls up to read
    c.fireScroll();
    expect(auto.pinned).toBe(false);
    c.setScrollHeight(1500);
    auto.maybeFollow();
    expect(c.scrollTop).toBe(200); // view stays where the user put it
  });

  it("jumpToLatest re-pins and scrolls to the bottom", () => {
    const c = fakeContainer({ scrollTop: 100 });
    const auto = createAutoScroll(c);
    c.fireScroll();
    expect(auto.pinned).toBe(false);
    auto.jumpToLatest();
    expect(auto.pinned).toBe(true);
    expect(c.scrollTop).toBe(1000);
    c.setScrollHeight(1400);
    auto.maybeFollow();
    expect(c.scrollTop).toBe(1400); // following resumes after the jump
  });

  it("destroy removes the scroll listener", () => {
    const c = fakeContainer();
    const auto = createAutoScroll(c);
    expect(c.listenerCount()).toBe(1);
    auto.destroy();
    expect(c.listenerCount()).toBe(0);
  });
});
