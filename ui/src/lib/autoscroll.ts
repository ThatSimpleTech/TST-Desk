// Auto-scroll logic (TD-1004).
//
// DOM-free and rune-free so it is unit-testable in node: the pane wraps the
// returned object in $state, which makes `pinned` reactive for the
// jump-to-latest button. Mutations go through the returned object itself, so
// a proxy wrapping it observes every change.

export const BOTTOM_THRESHOLD_PX = 48;

export interface ScrollContainerLike {
  scrollTop: number;
  readonly clientHeight: number;
  readonly scrollHeight: number;
  addEventListener(type: "scroll", listener: () => void, options?: { passive?: boolean }): void;
  removeEventListener(type: "scroll", listener: () => void): void;
}

export function isAtBottom(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
  threshold: number = BOTTOM_THRESHOLD_PX,
): boolean {
  return scrollHeight - (scrollTop + clientHeight) <= threshold;
}

export interface AutoScroll {
  pinned: boolean;
  /** Scroll to the bottom, but only while the user hasn't scrolled up. */
  maybeFollow(): void;
  /** User-driven: re-pin and scroll to the bottom. */
  jumpToLatest(): void;
  destroy(): void;
}

export function createAutoScroll(container: ScrollContainerLike): AutoScroll {
  function onScroll(): void {
    self.pinned = isAtBottom(container.scrollTop, container.clientHeight, container.scrollHeight);
  }

  const self: AutoScroll = {
    pinned: true,
    maybeFollow() {
      if (self.pinned) container.scrollTop = container.scrollHeight;
    },
    jumpToLatest() {
      self.pinned = true;
      container.scrollTop = container.scrollHeight;
    },
    destroy() {
      container.removeEventListener("scroll", onScroll);
    },
  };

  container.addEventListener("scroll", onScroll, { passive: true });
  return self;
}
