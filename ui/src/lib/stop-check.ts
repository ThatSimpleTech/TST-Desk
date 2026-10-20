// The done-check on an interactive turn (TD-5124), for the chat.
//
// Pure helpers: the daemon decides the outcome and sends it in a
// `stop_check` event; this only words it. The UI never judges a check.

import type { CheckBlock } from "./chat-message";
import type { StopCheck, StopCheckItem } from "./protocol";

export function checkFromEvent(event: StopCheck): CheckBlock {
  return {
    outcome: event.outcome,
    attempt: event.attempt,
    maxRetries: event.max_retries,
    items: event.items ?? [],
  };
}

/** The fold's one line. */
export function checkLabel(check: CheckBlock): string {
  switch (check.outcome) {
    case "passed":
      return "Done-check passed";
    case "retry":
      return `Done-check failed · try ${check.attempt} of ${check.maxRetries}, the agent keeps working`;
    case "failed":
      return check.maxRetries === 0
        ? "Done-check failed"
        : `Done-check still failing after ${check.maxRetries} ${check.maxRetries === 1 ? "try" : "tries"}`;
    case "unchecked":
      return "Done-check could not run";
  }
}

/** What a failed or unchecked check shows without a click. */
export function checkOpenByDefault(check: CheckBlock): boolean {
  return check.outcome === "failed" || check.outcome === "unchecked";
}

function itemName(item: StopCheckItem): string {
  return item.source === "goal" ? `Goal: ${item.item}` : item.item;
}

function mark(item: StopCheckItem): string {
  if (item.decided === false) return "?";
  return item.passed ? "✓" : "✗";
}

/** One line per item, its mark first; a failed item's detail under it. */
export function checkBody(check: CheckBlock): string {
  return check.items
    .map((item) => {
      const head = `${mark(item)} ${itemName(item)}`;
      const detail = (item.detail ?? "").trim();
      if (item.passed || detail === "") return head;
      const indented = detail
        .split("\n")
        .map((line) => `  ${line}`)
        .join("\n");
      return `${head}\n${indented}`;
    })
    .join("\n");
}
