// Elapsed-time wording, shared by everything that reports a duration.
//
// Its own module because two of its callers import each other: the chat store
// owns message state and the reasoning disclosure reads it, so a formatter
// living in either one puts a cycle between them (TD-1902).

/** A sub-second span still reads "1s" — "0s" would claim work didn't
 *  happen (TD-1607). */
export function formatDuration(seconds: number): string {
  const total = Math.max(1, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}m ${rest}s`;
}
