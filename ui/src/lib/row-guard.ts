// Destructive row actions cannot be done by the tail of the last one (INTERACT-12).
//
// A one-click Remove or Revoke closed the gap at once, so the next row slid
// under the unmoved pointer and a double-click's second press removed it as
// well: two stored keys, two policy rules. Every such action now asks first
// (ConfirmButton), and once one has fired, every other row action ignores
// input for ROW_QUIET_MS.

export const ROW_QUIET_MS = 400;

let quietUntil = 0;

/** A destructive row action just fired. */
export function rowActionFired(now: number = Date.now()): void {
  quietUntil = now + ROW_QUIET_MS;
}

/** True while row actions ignore input after one fired. */
export function rowsQuiet(now: number = Date.now()): boolean {
  return now < quietUntil;
}

export function resetRowGuard(): void {
  quietUntil = 0;
}
