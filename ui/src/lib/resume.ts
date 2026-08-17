// Resume detection — the DOM seam of resume healing (TD-1716).
//
// macOS App Nap suspends an occluded or backgrounded WKWebView's JavaScript:
// timers stop, socket frames queue at the OS, and the page freezes mid-frame
// with no error anywhere. That is legitimate power management and this app
// does not fight it (NSAppSleepDisabled and friends were rejected) — it heals
// on the way back instead.
//
// Two signals say the page is running again: `visibilitychange` landing on
// "visible", and the window taking focus. Either is a resume, and both firing
// is normal — healing is idempotent by design, since re-attaching twice costs
// a replayed frame the client drops on seq.
//
// The document and window come in as arguments so this stays pure and
// testable under vitest's node environment, with no DOM to stand up.

export interface ResumeDocument {
  readonly visibilityState: string;
  addEventListener(type: "visibilitychange", handler: () => void): void;
  removeEventListener(type: "visibilitychange", handler: () => void): void;
}

export interface ResumeWindow {
  addEventListener(type: "focus", handler: () => void): void;
  removeEventListener(type: "focus", handler: () => void): void;
}

/**
 * Call `onResume` whenever the page comes back to the foreground. Returns the
 * unsubscribe.
 *
 * `visibilitychange` fires on the way out too; only the "visible" edge is a
 * resume — healing on the way to sleep would just re-attach into a socket
 * about to be suspended.
 */
export function watchResume(
  doc: ResumeDocument,
  win: ResumeWindow,
  onResume: () => void,
): () => void {
  const onVisibility = (): void => {
    if (doc.visibilityState === "visible") onResume();
  };
  const onFocus = (): void => onResume();

  doc.addEventListener("visibilitychange", onVisibility);
  win.addEventListener("focus", onFocus);

  return () => {
    doc.removeEventListener("visibilitychange", onVisibility);
    win.removeEventListener("focus", onFocus);
  };
}
