// @vitest-environment jsdom
//
// CHAT-04: your own message is shown exactly as you typed and sent it, not
// run through markdown: <, #, __, * and line breaks all stay.

import { afterEach, describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";

import MessageBubble from "./components/chat/MessageBubble.svelte";

describe("your own message (CHAT-04)", () => {
  let app: ReturnType<typeof mount> | null = null;
  afterEach(() => {
    if (app) unmount(app);
    app = null;
    document.body.innerHTML = "";
  });

  it("is shown exactly as typed", () => {
    const text = "Why does Vec<String> not implement Copy?\n# not a heading\n__init__.py and 2*3*4\n<b>not bold</b>";
    app = mount(MessageBubble, {
      target: document.body,
      props: { message: { id: "m1", role: "user", text, complete: true, at: 0 }, first: true },
    });
    flushSync();
    const shown = document.querySelector(".user-text");
    expect(shown?.textContent).toBe(text);
    expect(shown?.querySelector("b, h1, strong, em")).toBeNull();
  });
});
