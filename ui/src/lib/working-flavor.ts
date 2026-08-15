// Working-state flavor (TD-1713).
//
// The first-token wait got personality: the shimmer cycles through a stable
// of verbs in tstd's voice — measured, warm, a little dry, like a shop
// foreman reporting progress. No emoji, no exclamation, never a claim about
// internals the client can't see. Pure functions only: ChatPane owns the
// ticking clock, tests pin the cadence exactly.
//
// The list deliberately avoids the Claude Code spinner's verbs — same trick,
// different shop.

/** One word, present participle, reads naturally before an ellipsis. */
export const WORKING_VERBS: readonly string[] = [
  "Whittling",
  "Tinkering",
  "Steeping",
  "Puttering",
  "Pottering",
  "Mulling",
  "Ruminating",
  "Sussing",
  "Finagling",
  "Fiddling",
  "Sifting",
  "Piddling",
];

/** How long one verb holds before the next rotates in. */
export const VERB_ROTATE_MS = 2_500;

/** The verb showing `elapsedMs` into the wait. Rotates on the cadence and
 *  wraps the list; clamps negatives to the first verb so a skewed clock
 *  never indexes out of range. */
export function workingVerb(elapsedMs: number): string {
  const step = Math.floor(Math.max(0, elapsedMs) / VERB_ROTATE_MS);
  return WORKING_VERBS[step % WORKING_VERBS.length];
}
