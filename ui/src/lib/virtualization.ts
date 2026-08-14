// Fixed-row window math for the virtualized activity timeline (AC #7).
//
// Renders only the slice of entries visible in the scroll viewport (plus an
// overscan buffer) with padding spacers above and below, so a thousand-entry
// session keeps the DOM small and stays responsive. Pure and DOM-free so it
// unit-tests under vitest's node environment (like splitpane.ts).

export interface VirtualWindow {
  /** First index to render (inclusive). */
  start: number;
  /** One past the last index to render. */
  end: number;
  /** Padding (px) above the rendered slice to preserve the scrollbar. */
  topPad: number;
  /** Padding (px) below the rendered slice. */
  bottomPad: number;
}

/** The number of extra rows rendered beyond the viewport on each side. */
export const OVERSCAN_ROWS = 5;

export function computeWindow(
  itemCount: number,
  rowHeight: number,
  scrollTop: number,
  viewportHeight: number,
  overscan = OVERSCAN_ROWS,
): VirtualWindow {
  if (itemCount <= 0 || rowHeight <= 0 || viewportHeight <= 0) {
    return { start: 0, end: 0, topPad: 0, bottomPad: 0 };
  }
  const firstVisible = Math.floor(scrollTop / rowHeight);
  const start = Math.max(0, firstVisible - overscan);
  const visibleRows = Math.ceil(viewportHeight / rowHeight) + overscan * 2;
  const end = Math.min(itemCount, start + visibleRows);
  return {
    start,
    end,
    topPad: start * rowHeight,
    bottomPad: (itemCount - end) * rowHeight,
  };
}
