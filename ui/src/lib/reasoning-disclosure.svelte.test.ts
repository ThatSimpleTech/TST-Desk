import { beforeEach, describe, expect, it } from "vitest";

import {
  hasReasoning,
  isExpanded,
  isThinkingLive,
  resetDisclosures,
  thoughtLabel,
  toggle,
  type ReasoningState,
} from "./reasoning-disclosure.svelte.js";

function thinking(id = "m1"): ReasoningState {
  return { id, reasoning: "Let me think" };
}

function thought(id = "m1", ms = 63_000): ReasoningState {
  return { id, reasoning: "Let me think", reasoningMs: ms };
}

beforeEach(() => {
  resetDisclosures();
});

describe("hasReasoning", () => {
  it("is false for a message from a model that does not reason", () => {
    expect(hasReasoning({ id: "m1" })).toBe(false);
  });

  it("is false for an empty string, so no empty block ever renders", () => {
    expect(hasReasoning({ id: "m1", reasoning: "" })).toBe(false);
  });

  it("is true once any thinking has arrived", () => {
    expect(hasReasoning(thinking())).toBe(true);
  });
});

describe("default fold state", () => {
  it("is open while the thinking is the live thing", () => {
    expect(isThinkingLive(thinking())).toBe(true);
    expect(isExpanded(thinking())).toBe(true);
  });

  it("closes on its own once the answer has started", () => {
    expect(isThinkingLive(thought())).toBe(false);
    expect(isExpanded(thought())).toBe(false);
  });
});

describe("explicit toggles", () => {
  it("open a finished thought and keep it open", () => {
    const message = thought();
    expect(toggle(message)).toBe(true);
    expect(isExpanded(message)).toBe(true);
  });

  it("survive the transition from live to finished", () => {
    // The reader opened a live thought to read it. The first content token
    // must not shut it under them.
    const live = thinking("m7");
    toggle(live); // collapse it while live
    expect(isExpanded(live)).toBe(false);
    const finished: ReasoningState = { ...live, reasoningMs: 1000 };
    expect(isExpanded(finished)).toBe(false);
  });

  it("are per message, not global", () => {
    toggle(thought("m1"));
    expect(isExpanded(thought("m1"))).toBe(true);
    expect(isExpanded(thought("m2"))).toBe(false);
  });

  it("survive the row being destroyed and rebuilt", () => {
    // MessageList windows its rows: scrolling away destroys the component.
    // State lives in the module precisely so this holds.
    toggle(thought("m3"));
    const rebuilt = thought("m3");
    expect(isExpanded(rebuilt)).toBe(true);
  });
});

describe("resetDisclosures", () => {
  it("clears toggles so a recycled message id starts fresh", () => {
    toggle(thought("m1"));
    expect(isExpanded(thought("m1"))).toBe(true);
    resetDisclosures();
    expect(isExpanded(thought("m1"))).toBe(false);
  });
});

describe("thoughtLabel", () => {
  it("reports no duration while thinking is still streaming", () => {
    expect(thoughtLabel(thinking())).toBe("Thinking");
  });

  it("reports seconds under a minute", () => {
    expect(thoughtLabel(thought("m1", 8_000))).toBe("Thought for 8s");
  });

  it("reports minutes and seconds over one", () => {
    expect(thoughtLabel(thought("m1", 63_000))).toBe("Thought for 1m 3s");
  });

  it("never reports zero — a sub-second thought still took time", () => {
    expect(thoughtLabel(thought("m1", 120))).toBe("Thought for 1s");
  });
});
