// Errors a form already answers in place (TD-5015).
//
// The connection hands each event to the stores first and to the notice sink
// last (connection-status.svelte.ts). A store that shows an error inline, such
// as Settings > Computers' Add form, claims that event here, so the sink does
// not raise a second, global notice whose copy was written for another flow
// (keychain_locked's names the API-key wizard). A leaf module so the sink
// never imports the stores that feed it.

const claimed = new WeakSet<object>();

/** Mark an event as shown inline. */
export function claimInline(event: object): void {
  claimed.add(event);
}

/** True when a store already showed this event inline. */
export function claimedInline(event: object): boolean {
  return claimed.has(event);
}
