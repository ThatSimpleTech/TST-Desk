// A delegated worker's transcript as text (TD-5117). The daemon sends the
// steps on the `delegate` call's tool_result; the parent model only ever
// got the summary, so this is the one place the user can read what the
// worker did. Shared by the chat fold and the activity timeline.
import type { WorkerStep } from "./protocol";

const CUT = "\n… (cut)";

function formatStep(step: WorkerStep): string {
  const cut = step.truncated === true ? CUT : "";
  const tool = step.tool ?? "tool";
  switch (step.kind) {
    case "task":
      return `Task:\n${step.text}${cut}`;
    case "reply":
      return `Worker:\n${step.text}${cut}`;
    case "tool_call":
      return `→ ${tool} ${step.text}${cut}`;
    case "tool_result":
      return `← ${tool}${step.status === "error" ? " (error)" : ""}\n${step.text}${cut}`;
    case "note":
      return `· ${step.text}`;
  }
}

/** The steps as one plain-text block, oldest first, blank line between. */
export function formatWorkerTranscript(steps: readonly WorkerStep[]): string {
  return steps.map(formatStep).join("\n\n");
}
