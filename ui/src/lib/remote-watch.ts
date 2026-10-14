// Timers behind a phone's saved connection (TD-5014). Kept apart from the
// connection store so the timing is testable with fake timers and a fake
// document, and the store stays about what is saved and shown.

/** When to look again at a saved connection the key store would not open. */
export const STORE_RETRY_DELAYS_MS: readonly number[] = [1_000, 5_000];

export interface VisibilityDoc {
  readonly visibilityState: string;
  addEventListener(type: "visibilitychange", fn: () => void): void;
  removeEventListener(type: "visibilitychange", fn: () => void): void;
}

/** Look again at the saved connection while Android's key store is not
 *  answering: at each delay, then whenever the app comes back to the front.
 *  `attempt` returns true once there is nothing left to retry; `settle` runs
 *  when the timed tries are spent and it still has not. */
export class StoreRetry {
  private timers: ReturnType<typeof setTimeout>[] = [];
  private doc: VisibilityDoc | null = null;
  private readonly onVisible = (): void => {
    if (this.doc?.visibilityState === "visible") this.retryNow();
  };

  constructor(
    private readonly attempt: () => boolean,
    private readonly settle: () => void,
  ) {}

  start(doc: VisibilityDoc | null, delays: readonly number[] = STORE_RETRY_DELAYS_MS): void {
    this.stop();
    delays.forEach((ms, i) => {
      const timer = setTimeout(() => {
        this.timers = this.timers.filter((t) => t !== timer);
        if (!this.retryNow() && i === delays.length - 1) this.settle();
      }, ms);
      this.timers.push(timer);
    });
    if (doc !== null) {
      this.doc = doc;
      doc.addEventListener("visibilitychange", this.onVisible);
    }
  }

  /** One try now. True when it worked, and then nothing more is scheduled. */
  retryNow(): boolean {
    if (!this.attempt()) return false;
    this.stop();
    return true;
  }

  stop(): void {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers = [];
    this.doc?.removeEventListener("visibilitychange", this.onVisible);
    this.doc = null;
  }
}

/** Flags a saved computer that has not answered for `afterMs`. Feed it every
 *  connection state; `watching` is "a phone with a saved computer". */
export class UnreachableWatch {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private on = false;

  constructor(
    private readonly afterMs: number,
    private readonly onChange: (unreachable: boolean) => void,
  ) {}

  update(state: string, watching: boolean): void {
    if (!watching || state === "connected" || state === "stopped") {
      this.reset();
      return;
    }
    if (this.timer !== null || this.on) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.on = true;
      this.onChange(true);
    }, this.afterMs);
  }

  reset(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (!this.on) return;
    this.on = false;
    this.onChange(false);
  }
}
