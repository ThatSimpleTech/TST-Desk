// Composer-side attachment vetting (TD-1709).
//
// This half is a courtesy: the daemon runs the same caps and the same
// text/binary test on arrival (core/tests/test_attachments.py), and that is
// the gate that actually holds. What is checked here is that the early
// refusal agrees with it — same conditions, same codes — so the composer
// never promises a send the daemon will refuse, or refuses one it would
// have taken.

import { describe, expect, it } from "vitest";
import {
  DEFAULT_ATTACHMENT_LIMITS,
  acceptAttachment,
  isImageBytes,
  formatBytes,
  isTextBytes,
  safeName,
  toBase64,
  toChips,
  toWireAttachments,
  totalBytes,
  type NewAttachment,
} from "./attachments";
import type { AttachmentLimits } from "./protocol";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);

function bytes(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function limits(over: Partial<AttachmentLimits> = {}): AttachmentLimits {
  return { ...DEFAULT_ATTACHMENT_LIMITS, ...over };
}

function draft(name: string, size: number): NewAttachment {
  return { name, size, content_b64: "" };
}

describe("isTextBytes", () => {
  it("takes plain UTF-8", () => {
    expect(isTextBytes(bytes("hello\nworld\n"))).toBe(true);
  });

  it("takes UTF-8 beyond ASCII", () => {
    expect(isTextBytes(bytes("café — 日本語"))).toBe(true);
  });

  it("takes an empty file", () => {
    expect(isTextBytes(new Uint8Array())).toBe(true);
  });

  it("refuses a PNG header", () => {
    expect(isTextBytes(PNG)).toBe(false);
  });

  it("refuses a NUL byte even though NUL is legal UTF-8", () => {
    expect(isTextBytes(new Uint8Array([0x61, 0x00, 0x62]))).toBe(false);
  });

  it("refuses a lone continuation byte", () => {
    // Without `fatal` this decodes to U+FFFD and reads as ordinary prose.
    expect(isTextBytes(new Uint8Array([0x41, 0x80, 0x42]))).toBe(false);
  });
});

describe("acceptAttachment", () => {
  it("accepts a text file inside the caps", () => {
    const outcome = acceptAttachment("notes.md", bytes("# Title\n"), limits());
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.draft.name).toBe("notes.md");
    expect(outcome.draft.size).toBe(8);
    expect(atob(outcome.draft.content_b64)).toBe("# Title\n");
  });

  it("accepts a PNG header as an image", () => {
    expect(isImageBytes(PNG)).toBe(true);
  });

  it("refuses an image without vision with copy naming config", () => {
    const outcome = acceptAttachment("shot.png", PNG, limits());
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_no_vision");
    expect(outcome.refusal.message).toContain("shot.png");
    expect(outcome.refusal.message).toContain("vision: true");
  });

  it("accepts JPEG, GIF, and WebP headers as images when vision is enabled", () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0]);
    const gif = new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61]);
    const webp = new Uint8Array([
      0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50,
    ]);
    expect(acceptAttachment("a.jpg", jpeg, limits(), [], true).ok).toBe(true);
    expect(acceptAttachment("a.gif", gif, limits(), [], true).ok).toBe(true);
    expect(acceptAttachment("a.webp", webp, limits(), [], true).ok).toBe(true);
  });

  it("refuses a binary file with copy naming it and why", () => {
    const outcome = acceptAttachment("blob.dat", new Uint8Array([0, 1, 2, 3, 4]), limits());
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_binary");
    expect(outcome.refusal.message).toContain("blob.dat");
  });

  it("accepts an image when vision is enabled", () => {
    const outcome = acceptAttachment("shot.png", PNG, limits(), [], true);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.draft.name).toBe("shot.png");
  });

  it("still refuses non-image binary when vision is enabled", () => {
    const outcome = acceptAttachment("blob.dat", new Uint8Array([0, 1, 2, 3]), limits(), [], true);
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_binary");
  });

  it("refuses an oversize file and names the config key", () => {
    const outcome = acceptAttachment("big.txt", bytes("x".repeat(11)), limits({ max_file_bytes: 10 }));
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_too_large");
    expect(outcome.refusal.message).toContain("max_file_bytes");
    expect(outcome.refusal.message).toContain("big.txt");
  });

  it("takes a file exactly at the per-file cap", () => {
    const outcome = acceptAttachment("ten.txt", bytes("x".repeat(10)), limits({ max_file_bytes: 10 }));
    expect(outcome.ok).toBe(true);
  });

  it("counts what is already staged toward the total", () => {
    const outcome = acceptAttachment(
      "next.txt",
      bytes("x".repeat(6)),
      limits({ max_total_bytes: 10 }),
      [draft("first.txt", 5)],
    );
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_total_too_large");
    expect(outcome.refusal.message).toContain("max_total_bytes");
  });

  it("counts what is already staged toward the file count", () => {
    const outcome = acceptAttachment("third.txt", bytes("x"), limits({ max_count: 2 }), [
      draft("a.txt", 1),
      draft("b.txt", 1),
    ]);
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_too_many");
    expect(outcome.refusal.message).toContain("max_count");
  });

  it("pluralises the count refusal honestly", () => {
    const one = acceptAttachment("b.txt", bytes("x"), limits({ max_count: 1 }), [draft("a.txt", 1)]);
    expect(one.ok).toBe(false);
    if (one.ok) return;
    expect(one.refusal.message).toContain("attach 1 file to one message");
  });

  it("checks size before text, so a huge binary reports its size", () => {
    // Reading a 10MB PNG as text to decide it is binary is work nobody needs.
    const huge = new Uint8Array(64);
    huge.set(PNG);
    const outcome = acceptAttachment("shot.png", huge, limits({ max_file_bytes: 8 }));
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_too_large");
  });

  it("never claims anything was sent — nothing has been", () => {
    const cases = [
      acceptAttachment("blob.dat", new Uint8Array([0, 1, 2, 3, 4]), limits()),
      acceptAttachment("big.txt", bytes("xx"), limits({ max_file_bytes: 1 })),
      acceptAttachment("n.txt", bytes("x"), limits({ max_count: 1 }), [draft("a.txt", 1)]),
    ];
    for (const outcome of cases) {
      expect(outcome.ok).toBe(false);
      if (outcome.ok) continue;
      expect(outcome.refusal.message).not.toContain("was sent");
    }
  });

  it("shows the basename, matching what the daemon will accept", () => {
    const outcome = acceptAttachment("../../etc/passwd", bytes("root\n"), limits());
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.draft.name).toBe("passwd");
  });
});

describe("helpers", () => {
  it("strips both separators from a name", () => {
    expect(safeName("a/b/c.txt")).toBe("c.txt");
    expect(safeName("C:\\Users\\me\\c.txt")).toBe("c.txt");
    expect(safeName("plain.txt")).toBe("plain.txt");
  });

  it("round-trips bytes through base64", () => {
    const source = bytes("café — 日本語\n");
    expect(new TextEncoder().encode(atob(toBase64(source))).length).toBeGreaterThan(0);
    expect(atob(toBase64(bytes("hello")))).toBe("hello");
  });

  it("base64s a file larger than one chunk", () => {
    const big = new Uint8Array(0x8000 * 2 + 7).fill(65);
    expect(atob(toBase64(big)).length).toBe(big.length);
  });

  it("formats sizes the way the daemon's copy does", () => {
    expect(formatBytes(12)).toBe("12 bytes");
    expect(formatBytes(1500)).toBe("1.5 KB");
    expect(formatBytes(2_500_000)).toBe("2.5 MB");
  });

  it("adds staged bytes", () => {
    expect(totalBytes([draft("a", 3), draft("b", 4)])).toBe(7);
    expect(totalBytes([])).toBe(0);
  });

  it("strips the bytes off a chip", () => {
    expect(toChips([{ name: "a.txt", size: 3, content_b64: "eA==" }])).toEqual([
      { name: "a.txt", size: 3 },
    ]);
  });

  it("sends only what the wire declares", () => {
    expect(toWireAttachments([{ name: "a.txt", size: 3, content_b64: "eA==" }])).toEqual([
      { name: "a.txt", content_b64: "eA==" },
    ]);
  });
});

describe("the defaults mirror the daemon's", () => {
  it("matches core/tstd/attachments.py", () => {
    // Two hand-kept sides, like the protocol itself. A drift here means the
    // composer promises a send the daemon refuses.
    expect(DEFAULT_ATTACHMENT_LIMITS).toEqual({
      max_file_bytes: 256_000,
      max_total_bytes: 512_000,
      max_count: 10,
    });
  });
});
