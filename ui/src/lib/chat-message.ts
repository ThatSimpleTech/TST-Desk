// One transcript row and its parts (TD-1004, TD-1902).
//
// Split out of chat-store.ts so the store stays the wiring (TD-5019 grew it
// past the file budget). Every importer still reaches these through
// chat-store, which re-exports them.

import type { AttachmentChip } from "./attachments";
import type { ResponseMark } from "./chat-retract";
import type { SentFileRef } from "./sent-files";
import type { WorkerStep } from "./protocol";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** A reasoning model's thinking for this message (TD-1901), kept apart
   *  from `text` because it is not the answer: TD-1902 folds it behind a
   *  disclosure, and nothing may ever concatenate the two. Absent for
   *  models that do not reason. */
  reasoning?: string;
  /** Epoch ms of the first reasoning delta, so the disclosure can label
   *  itself with a duration. Cleared to a final elapsed by `reasoningMs`
   *  once content starts. */
  reasoningStartedAt?: number;
  /** How long reasoning ran, once the answer began. Undefined while it is
   *  still streaming — that is what distinguishes live from finished. */
  reasoningMs?: number;
  complete: boolean;
  /** Epoch ms when the client first saw the message (send echo or first
   *  delta). Replayed history stamps attach time — display-only (TD-1606). */
  at: number;
  /** Files sent with a user row, rendered as chips (TD-1709). Names and
   *  sizes only: the bytes are gone the moment they are on the wire, and
   *  keeping them would hold the whole session's attachments in memory for
   *  a transcript that only ever shows the label. */
  attachments?: AttachmentChip[];
  /** Tool calls that ran during this assistant turn (TD-1902). Folded
   *  like reasoning so a tool-heavy turn does not grow the transcript
   *  without bound. Absent when the turn used no tools. Flat mirror of the
   *  tool entries in `parts` — same object references, so a result update
   *  lands in both. Kept for lookup and tests; rendering follows `parts`. */
  tools?: ToolBlock[];
  /** Ordered content of an assistant row: text segments and tool calls in
   *  arrival order, so a tool that ran after the last text renders after it
   *  instead of above the whole answer. Text deltas append to the trailing
   *  text part, or open a new one after a tool. `text` stays the
   *  concatenation of the text parts for copy and tests. Absent on user
   *  rows and on assistant rows that never streamed content or tools. */
  parts?: ChatPart[];
  /** 0-based index among user turns (TD-1708). Only on user rows. */
  userIndex?: number;
  siblingIndex?: number;
  siblingCount?: number;
  /** Daemon-issued id for this user turn. Stamps a local echo so replay
   *  of `user_turn` does not duplicate the row. */
  turnId?: string;
  /** Where the latest model response began on this assistant row, so an
   *  `assistant_retract` can take it back off (TD-5159, chat-retract.ts). */
  responseMark?: ResponseMark;
}

/** One ordered entry on an assistant row: a text segment or a tool call.
 *  Text splits only at tool boundaries — one part holds everything said
 *  before the tool, the next everything after — so markdown within a
 *  segment renders intact. */
export type ChatPart = { kind: "text"; text: string } | { kind: "tool"; tool: ToolBlock };

/** One tool call on an assistant row (TD-1902). `status` is unset while
 *  the call is still running — that is what keeps the disclosure open. */
export interface ToolBlock {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  decisionClass?: "A" | "B" | "C" | null;
  status?: "success" | "error";
  output?: string;
  errorCode?: string | null;
  /** Files a `send_file` call put in the chat (TD-5144), in arrival order.
   *  Filled from the session's `artifact_ready` events, which replay on
   *  attach, so the cards come back with the transcript. */
  files?: SentFileRef[];
  /** A `delegate` call's worker transcript (TD-5117), from its result. */
  transcript?: WorkerStep[];
}
