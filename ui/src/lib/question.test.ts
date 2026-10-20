// TD-5121: the question-card model. Picks, readiness and the messages the
// card sends, with no DOM.

import { describe, expect, it } from "vitest";
import fixtures from "./protocol-fixtures.json";
import type { QuestionRequest } from "./protocol";
import {
  answerMessage,
  canSend,
  declineMessage,
  emptyDrafts,
  questionFromEvent,
  sendsOnTap,
  toggle,
} from "./question";

const EVENT = fixtures.question_request as QuestionRequest;
const PENDING = questionFromEvent(EVENT);
const [DB, FEATURES] = PENDING.questions as [typeof PENDING.questions[0], typeof PENDING.questions[0]];

describe("question model", () => {
  it("reads the event into a card", () => {
    expect(PENDING).toEqual({ sessionId: "sess-1", toolCallId: "tc-q", questions: EVENT.questions });
    expect(emptyDrafts(PENDING)).toEqual([
      { selected: [], text: "" },
      { selected: [], text: "" },
    ]);
  });

  it("a single-choice tap replaces the last pick, and a second tap clears it", () => {
    let draft = toggle(DB, { selected: [], text: "" }, "Postgres");
    expect(draft.selected).toEqual(["Postgres"]);
    draft = toggle(DB, draft, "SQLite");
    expect(draft.selected).toEqual(["SQLite"]);
    expect(toggle(DB, draft, "SQLite").selected).toEqual([]);
  });

  it("a multi-choice tap adds or removes, in the options' order", () => {
    let draft = toggle(FEATURES, { selected: [], text: "" }, "Search");
    draft = toggle(FEATURES, draft, "Auth");
    expect(draft.selected).toEqual(["Auth", "Search"]);
    expect(toggle(FEATURES, draft, "Search").selected).toEqual(["Auth"]);
  });

  it("is ready once every question has a pick or the person's own words", () => {
    expect(canSend([{ selected: ["Postgres"], text: "" }, { selected: [], text: "  " }])).toBe(false);
    expect(canSend([{ selected: ["Postgres"], text: "" }, { selected: [], text: "audit logs" }])).toBe(true);
    expect(canSend([])).toBe(false);
  });

  it("one single-choice question sends on a tap; anything more waits for Send", () => {
    expect(sendsOnTap(PENDING)).toBe(false);
    expect(sendsOnTap({ ...PENDING, questions: [DB] })).toBe(true);
    expect(sendsOnTap({ ...PENDING, questions: [FEATURES] })).toBe(false);
  });

  it("builds answer_question with trimmed words, and the decline", () => {
    const drafts = [
      { selected: ["SQLite"], text: "" },
      { selected: ["Auth"], text: "  soon " },
    ];
    expect(answerMessage(PENDING, drafts)).toEqual({
      type: "answer_question",
      session_id: "sess-1",
      tool_call_id: "tc-q",
      answers: [
        { selected: ["SQLite"], text: "" },
        { selected: ["Auth"], text: "soon" },
      ],
    });
    expect(declineMessage(PENDING)).toEqual({
      type: "answer_question",
      session_id: "sess-1",
      tool_call_id: "tc-q",
      answers: [],
      declined: true,
    });
  });
});
