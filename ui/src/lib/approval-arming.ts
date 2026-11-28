// The approval card's guard against the tail of the previous gesture
// (INTERACT-02, APPROVE-01).
//
// A turn with several gated calls shows them one at a time, each card in the
// spot the last one left, under the unmoved pointer. Without a guard the
// second click of a double-click, a second Enter or a held Enter approved a
// command nobody had read. So a card is inert for ARM_MS after it appears, a
// pointer click counts only when its press began after that, a repeated key
// never decides, and plain Enter is never Approve: Ctrl+Enter (⌘+Enter on a
// Mac) is, from inside the card.

/** How long a new card ignores input. */
export const ARM_MS = 400;

export interface ArmingState {
  armed: boolean;
  /** A pointer press began on a decision button after the card armed. */
  pressed: boolean;
}

export function newArming(): ArmingState {
  return { armed: false, pressed: false };
}

/** A pointerdown on a decision button. */
export function notePress(state: ArmingState): ArmingState {
  return { ...state, pressed: state.armed };
}

/**
 * Whether a click on a decision button decides. `detail` is the click's
 * count: 0 for a keyboard activation of a button the person focused on
 * purpose, which needs no press.
 */
export function clickDecides(state: ArmingState, detail: number): boolean {
  if (!state.armed) return false;
  return detail === 0 || state.pressed;
}

export interface KeyLike {
  key: string;
  repeat: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

export type CardKey = "approve" | "deny" | "swallow" | null;

/**
 * What a keydown inside the card does. "swallow" stops a key that must not
 * act: any auto-repeat, and a decision key before the card armed.
 */
export function cardKey(state: ArmingState, event: KeyLike): CardKey {
  if (event.repeat) return "swallow";
  const approveChord =
    event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey;
  const denyKey = event.key === "Escape";
  if (!approveChord && !denyKey) return null;
  if (!state.armed) return "swallow";
  return approveChord ? "approve" : "deny";
}
