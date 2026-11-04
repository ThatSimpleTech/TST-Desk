// The first-token wait and its watchdog (TD-1713, TD-1716).
//
// Between a user send and the first assistant_delta the pane claims "Working…".
// That claim needs an expiry: the 2026-08-14 stall sat silent for an hour still
// promising work was happening. So a wait carries a timer, and past the
// threshold the copy stops claiming and says what is actually known.
//
// This module owns the timer and the state fields the shimmer reads, and
// nothing else. It knows no protocol — no session id, no events, no wire (a
// retry's reason is held as an opaque string for the pane to word). The
// store decides *when* a wait begins and ends, from turn evidence; this decides
// what a wait already in progress is worth.

/** The slice of ChatState the wait owns. Everything else on the state is the
 *  store's; nothing here reads or writes past these fields. */
export interface FirstTokenWaitState {
  awaitingFirstToken: boolean;
  turnStalled: boolean;
  awaitingSince: number | null;
  /** This wait is for a reply the daemon withdrew and is asking for again
   *  (TD-5159), not for a send: the Working line says so. */
  retryingReply: boolean;
  /** Why that reply was withdrawn, as the daemon named it, so the Working
   *  line can say what the retry changes (TD-5233). Null when not retrying. */
  retryReason: string | null;
}

/** First-token watchdog (TD-1713): a wait this long with no assistant_delta
 *  and no terminal turn event stops claiming "Working" and says so. Long
 *  enough that a cold model on a big prompt stays unflagged; short enough
 *  that the 2026-08-14 silent stall could not sit an hour unremarked. */
export const STALL_TIMEOUT_MS = 25_000;

export interface FirstTokenWait {
  /** Start waiting for the first token, unless a wait is already running.
   *  `retryReason` marks a wait for a withdrawn reply asked again, and says
   *  why it was withdrawn (TD-5159, TD-5233). */
  begin(retryReason?: string): void;
  /** Stop waiting — the token landed, the turn resolved, or the session the
   *  wait belonged to went away. Drops the stall flag with it. */
  end(): void;
  /** Re-judge a wait in progress from the wall clock, after a suspension the
   *  timer slept through (TD-1716). */
  resume(): void;
}

/** One wait per store: the timer is closure state, so a store that is disposed
 *  or rebound leaves nothing armed behind it. */
export function createFirstTokenWait(state: FirstTokenWaitState): FirstTokenWait {
  // One outstanding timer at a time; every transition out of the wait clears it.
  let stallTimer: ReturnType<typeof setTimeout> | null = null;

  function clearStallTimer(): void {
    if (stallTimer !== null) {
      clearTimeout(stallTimer);
      stallTimer = null;
    }
  }

  /** `delay` is the remaining wait, which a resume shortens (TD-1716). */
  function arm(delay: number = STALL_TIMEOUT_MS): void {
    clearStallTimer();
    stallTimer = setTimeout(() => {
      stallTimer = null;
      if (state.awaitingFirstToken) state.turnStalled = true;
    }, delay);
    // Under node/vitest the timer is a Timeout object; unref so a pending
    // watchdog never holds a test process open. Browsers return a number.
    (stallTimer as unknown as { unref?: () => void }).unref?.();
  }

  return {
    /** Stamps the elapsed basis and arms the watchdog. A wait already in
     *  progress keeps its original stamp — a replayed "running" must not
     *  restart the user's clock. */
    begin(retryReason?: string): void {
      if (state.awaitingFirstToken) {
        if (retryReason !== undefined) {
          state.retryingReply = true;
          state.retryReason = retryReason;
        }
        return;
      }
      state.awaitingFirstToken = true;
      state.turnStalled = false;
      state.awaitingSince = Date.now();
      state.retryingReply = retryReason !== undefined;
      state.retryReason = retryReason ?? null;
      arm();
    },

    end(): void {
      state.awaitingFirstToken = false;
      state.turnStalled = false;
      state.awaitingSince = null;
      state.retryingReply = false;
      state.retryReason = null;
      clearStallTimer();
    },

    /** A suspended webview's timers do not fire, and the OS hands back the
     *  backlog coalesced into one late tick — so on the way back the timer is
     *  worth nothing and the wall clock is worth everything. A wait already
     *  past the threshold says so now rather than after a timer that may be
     *  another 25s away; a wait still inside it re-arms for what is left. */
    resume(): void {
      if (!state.awaitingFirstToken || state.awaitingSince === null) return;
      const waited = Date.now() - state.awaitingSince;
      if (waited >= STALL_TIMEOUT_MS) {
        state.turnStalled = true;
        clearStallTimer();
        return;
      }
      arm(STALL_TIMEOUT_MS - waited);
    },
  };
}
