// Narrow-viewport chrome (TD-3701).
//
// One AppShell, not a second mobile product. Below this width the rail and
// inspector hide so the phone is chat + approval. Either pane can be shown
// again; the default is one pane.

/** CSS `@media (max-width: 639px)` must stay one pixel below this. */
export const NARROW_VIEWPORT_PX = 640;

export function isNarrowViewport(width: number): boolean {
  return width < NARROW_VIEWPORT_PX;
}

export function narrowMediaQuery(): string {
  return `(max-width: ${NARROW_VIEWPORT_PX - 1}px)`;
}

export interface ShellChrome {
  rail: boolean;
  inspector: boolean;
}

/** Which chrome panes to paint. Wide always shows both. */
export function shellChrome(
  width: number,
  opts: { showRail?: boolean; showInspector?: boolean } = {},
): ShellChrome {
  if (!isNarrowViewport(width)) {
    return { rail: true, inspector: true };
  }
  return {
    rail: Boolean(opts.showRail),
    inspector: Boolean(opts.showInspector),
  };
}
