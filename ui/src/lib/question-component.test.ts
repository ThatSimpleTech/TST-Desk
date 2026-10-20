// @vitest-environment jsdom
//
// TD-5121: the question card, mounted for real. Options are buttons (taps
// on the phone, which shows this same card), the person may type their own
// answer, and Skip declines. Every button only sends the person's answer.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as unknown[], up: true };
});

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: unknown) => {
    if (!connection.up) return false;
    connection.sent.push(msg);
    return true;
  },
}));

import QuestionBar from "./components/QuestionBar.svelte";
import fixtures from "./protocol-fixtures.json";
import type { Error as DaemonError, QuestionAnswered, QuestionRequest, ToolResult, TurnComplete } from "./protocol";
import { bindQuestions, pending } from "./question-store.svelte.js";

const TWO = { ...(fixtures.question_request as QuestionRequest), session_id: "s1" };
const ONE: QuestionRequest = { ...TWO, tool_call_id: "tc-one", questions: [TWO.questions[0]!] };

type Event = QuestionRequest | QuestionAnswered | ToolResult | TurnComplete | DaemonError;

function emit(event: Event): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  connection.sent.length = 0;
  connection.up = true;
  bindQuestions(null);
  bindQuestions("s1");
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(QuestionBar, { target });
  flushSync();
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
});

function button(label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll("button")].find((b) =>
    (b.textContent ?? "").replace(/\s+/g, " ").trim().startsWith(label),
  );
  expect(found, label).toBeDefined();
  return found!;
}

function click(label: string): void {
  button(label).click();
  flushSync();
}

function type(index: number, text: string): void {
  const input = target.querySelectorAll<HTMLInputElement>("input.own")[index]!;
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  flushSync();
}

describe("QuestionCard", () => {
  it("shows nothing until there is a question", () => {
    expect(target.querySelector("article")).toBeNull();
  });

  it("shows each question with its options as buttons", () => {
    emit(TWO);
    expect(target.textContent).toContain("The agent has 2 questions");
    expect(target.textContent).toContain("Which database?");
    expect(target.textContent).toContain("Database");
    expect(target.textContent).toContain("Pick any that apply");
    for (const label of ["Postgres", "SQLite", "Auth", "Billing", "Search", "Skip", "Send answer"]) {
      expect(button(label)).toBeDefined();
    }
    expect(target.querySelectorAll("input.own")).toHaveLength(2);
    expect(connection.sent).toEqual([]);
  });

  it("one single-choice question is answered by one tap", () => {
    emit(ONE);
    click("SQLite");
    expect(connection.sent).toEqual([
      {
        type: "answer_question",
        session_id: "s1",
        tool_call_id: "tc-one",
        answers: [{ selected: ["SQLite"], text: "" }],
      },
    ]);
    // The card waits for the daemon, and takes no second answer.
    expect(target.textContent).toContain("Sending your answer");
    expect(button("Skip").disabled).toBe(true);
    emit({ type: "question_answered", session_id: "s1", tool_call_id: "tc-one", outcome: "answered", answers: [], seq: 9 });
    expect(target.querySelector("article")).toBeNull();
  });

  it("several questions wait for Send, with picks and the person's own words", () => {
    emit(TWO);
    expect(button("Send answer").disabled).toBe(true);
    click("Postgres");
    click("Auth");
    click("Search");
    expect(button("Auth").getAttribute("aria-pressed")).toBe("true");
    type(1, "and audit logs");
    expect(connection.sent).toEqual([]);
    click("Send answer");
    expect(connection.sent).toEqual([
      {
        type: "answer_question",
        session_id: "s1",
        tool_call_id: "tc-q",
        answers: [
          { selected: ["Postgres"], text: "" },
          { selected: ["Auth", "Search"], text: "and audit logs" },
        ],
      },
    ]);
  });

  it("own words alone answer a question", () => {
    emit(ONE);
    type(0, "MySQL");
    click("Send answer");
    expect(connection.sent).toEqual([
      { type: "answer_question", session_id: "s1", tool_call_id: "tc-one", answers: [{ selected: [], text: "MySQL" }] },
    ]);
  });

  it("Skip and Esc decline", () => {
    emit(TWO);
    click("Skip");
    expect(connection.sent).toEqual([
      { type: "answer_question", session_id: "s1", tool_call_id: "tc-q", answers: [], declined: true },
    ]);
    emit({ ...ONE });
    const card = target.querySelector<HTMLElement>('[data-question-card="tc-one"]')!;
    card.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    flushSync();
    expect(connection.sent).toHaveLength(2);
    expect(connection.sent[1]).toMatchObject({ tool_call_id: "tc-one", declined: true });
  });

  it("a refused answer comes back to the card with the daemon's words", () => {
    emit(ONE);
    click("Postgres");
    emit({
      type: "error",
      code: "bad_answer",
      message: "Question 1 has no option 'Postgres'.",
      session_id: "s1",
      in_reply_to: "answer_question",
      seq: 1,
    });
    expect(target.textContent).toContain("Question 1 has no option 'Postgres'.");
    expect(button("Skip").disabled).toBe(false);
  });

  it("an answer that could not be sent says so and stays", () => {
    connection.up = false;
    emit(ONE);
    click("SQLite");
    expect(connection.sent).toEqual([]);
    expect(target.textContent).toContain("Not sent");
    expect(pending).toHaveLength(1);
  });

  it("the call's result or the end of the turn takes the card away", () => {
    emit(TWO);
    emit({ type: "tool_result", session_id: "s1", tool_call_id: "tc-q", status: "error", output: "", truncated: false, error_code: "question_stopped", seq: 9 });
    expect(target.querySelector("article")).toBeNull();
    emit(TWO);
    emit({ type: "turn_complete", session_id: "s1", tokens: 0, cost: 0, tier: "brain", duration: 0, failed: true, seq: 10 } as TurnComplete);
    expect(target.querySelector("article")).toBeNull();
  });

  it("another session's question is not shown", () => {
    emit({ ...TWO, session_id: "s2" });
    expect(target.querySelector("article")).toBeNull();
  });
});
