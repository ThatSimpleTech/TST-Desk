// Auto-scroll logic (TD-1004).
//
// DOM-free so it is unit-testable in node. `pinned` is reactive because it is
// backed by `$state` inside the factory, which is why this file is a
// `.svelte.ts` module. A plain rune-free object wrapped in the pane's `$state`
// does NOT work: the factory's methods close over the raw target and mutate it
// directly, bypassing the proxy, so the jump button's `!auto.pinned` read never
// re-runs and the button never appears.

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
  // Backed by $state so `pinned` is reactive within this module; the factory's
  // own methods read/write it through `self.pinned` (the getter below), which
  // is what keeps the jump-to-latest button reactive.
  const pinnedState = $state({ pinned: true });

  function onScroll(): void {
    pinnedState.pinned = isAtBottom(container.scrollTop, container.clientHeight, container.scrollHeight);
  }

  const self: AutoScroll = {
    get pinned() {
      return pinnedState.pinned;
    },
    set pinned(v: boolean) {
      pinnedState.pinned = v;
    },
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
