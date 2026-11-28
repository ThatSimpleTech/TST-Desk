// Open a web link in the system browser, and say so when that fails (SETREST-01).
//
// The packaged app never granted the opener plugin's open_url permission, so
// every external link (docs, a link in a reply, the releases page) did
// nothing: the call was refused and the promise rejection went nowhere. The
// shell now grants it for https and http; if an open still fails, the person
// gets the address on the clipboard and a toast instead of silence.

import { notify } from "./notifications.svelte.js";
import { isTauri } from "./open-file";

/** Open *url*; on failure copy it and say so. True when it opened. */
export async function openExternal(url: string): Promise<boolean> {
  if (!isTauri()) {
    if (typeof window !== "undefined") window.open(url, "_blank", "noopener");
    return true;
  }
  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
    return true;
  } catch {
    let copied = false;
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
    } catch {
      copied = false;
    }
    notify(`open-url:${url}`, {
      severity: "toast",
      title: "Couldn't open the link",
      body: copied
        ? `The address is on your clipboard to paste into a browser: ${url}`
        : `Paste this address into a browser: ${url}`,
    });
    return false;
  }
}
