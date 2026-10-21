// PDF, DOCX and XLSX attachments in the composer (TD-5131).
//
// The composer cannot read a document, so it stages it as pending and asks
// the daemon (`extract_attachment`). These pin the chip's copy, the caps
// measured against the text the daemon reports, and the answer's folding.

import { describe, expect, it } from "vitest";
import {
  DEFAULT_ATTACHMENT_LIMITS,
  MAX_DOCUMENT_FILE_BYTES,
  acceptAttachment,
  applyExtracted,
  chipDetail,
  detectDocument,
  extractRequest,
  formatTokens,
  toChips,
  totalBytes,
  unmeasurePending,
  type AttachmentDraft,
} from "./attachments";
import type { AttachmentExtracted } from "./protocol";

const PDF = new TextEncoder().encode("%PDF-1.4\n%âã\n1 0 obj");
const ZIP = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00]);

function answer(over: Partial<AttachmentExtracted>): AttachmentExtracted {
  return {
    type: "attachment_extracted",
    seq: 1,
    session_id: "s1",
    request_id: "a1",
    name: "r.pdf",
    kind: "pdf",
    units: 3,
    text_bytes: 5000,
    tokens: 1250,
    error_code: null,
    message: "",
    ...over,
  };
}

function staged(id: string, name: string, bytes: Uint8Array): AttachmentDraft {
  const outcome = acceptAttachment(name, bytes, DEFAULT_ATTACHMENT_LIMITS);
  if (!outcome.ok) throw new Error(outcome.refusal.message);
  return { ...outcome.draft, id };
}

describe("detectDocument", () => {
  it("knows a PDF by its header and a zip by its name", () => {
    expect(detectDocument("anything.bin", PDF)).toBe("pdf");
    expect(detectDocument("Notes.DOCX", ZIP)).toBe("docx");
    expect(detectDocument("stock.xlsx", ZIP)).toBe("xlsx");
    expect(detectDocument("archive.zip", ZIP)).toBeNull();
    expect(detectDocument("fake.pdf", new TextEncoder().encode("hello"))).toBeNull();
  });

  it("knows a macro-enabled or unnamed document by what the zip holds (TD-5161)", () => {
    const holding = (member: string) =>
      new Uint8Array([...ZIP, ...new TextEncoder().encode(`....${member}....`)]);
    expect(detectDocument("Report.docm", holding("word/document.xml"))).toBe("docx");
    expect(detectDocument("budget.xlsm", holding("xl/workbook.xml"))).toBe("xlsx");
    expect(detectDocument("download", holding("xl/workbook.xml"))).toBe("xlsx");
    expect(detectDocument("archive.zip", holding("readme.txt"))).toBeNull();
  });
});

describe("staging a document", () => {
  it("was refused as binary before; now it is staged pending", () => {
    const outcome = acceptAttachment("r.pdf", PDF, DEFAULT_ATTACHMENT_LIMITS);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.draft.document).toBe("pdf");
    expect(outcome.draft.measure).toEqual({ state: "pending" });
  });

  it("takes a file bigger than max_file_bytes: the cap measures the text", () => {
    const big = new Uint8Array(5_000);
    big.set(PDF);
    const outcome = acceptAttachment("big.pdf", big, { ...DEFAULT_ATTACHMENT_LIMITS, max_file_bytes: 1_000 });
    expect(outcome.ok).toBe(true);
  });

  it("refuses a document past the transport's ceiling", () => {
    const huge = new Uint8Array(MAX_DOCUMENT_FILE_BYTES + 1);
    huge.set(PDF);
    const outcome = acceptAttachment("huge.pdf", huge, DEFAULT_ATTACHMENT_LIMITS);
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.refusal.code).toBe("attachment_too_large");
  });

  it("asks the daemon with the draft's id", () => {
    const draft = staged("a7", "r.pdf", PDF);
    const ask = extractRequest("s1", draft);
    expect(ask).toEqual({
      type: "extract_attachment",
      session_id: "s1",
      request_id: "a7",
      attachment: { name: "r.pdf", content_b64: draft.content_b64 },
    });
  });
});

describe("the daemon's answer", () => {
  it("fills the chip with pages and a token count", () => {
    const drafts = [staged("a1", "r.pdf", PDF)];
    expect(chipDetail(toChips(drafts)[0])).toBe("PDF · reading…");
    const { drafts: after, refusal } = applyExtracted(drafts, answer({}), DEFAULT_ATTACHMENT_LIMITS);
    expect(refusal).toBeNull();
    expect(chipDetail(toChips(after)[0])).toBe("PDF · 3 pages · ≈1.2k tokens");
    expect(totalBytes(after)).toBe(5000);
  });

  it("names sheets for a spreadsheet and nothing extra for a Word file", () => {
    const xlsx = [staged("a1", "s.xlsx", ZIP)];
    const one = applyExtracted(xlsx, answer({ kind: "xlsx", units: 1, tokens: 12 }), DEFAULT_ATTACHMENT_LIMITS);
    expect(chipDetail(toChips(one.drafts)[0])).toBe("XLSX · 1 sheet · ≈12 tokens");
    const docx = [staged("a1", "n.docx", ZIP)];
    const two = applyExtracted(docx, answer({ kind: "docx", units: 0, tokens: 1 }), DEFAULT_ATTACHMENT_LIMITS);
    expect(chipDetail(toChips(two.drafts)[0])).toBe("DOCX · ≈1 token");
  });

  it("drops the chip and says why on a refusal", () => {
    const drafts = [staged("a1", "scan.pdf", PDF)];
    const { drafts: after, refusal } = applyExtracted(
      drafts,
      answer({ error_code: "attachment_no_text", message: "scan.pdf has no text to read.", tokens: 0 }),
      DEFAULT_ATTACHMENT_LIMITS,
    );
    expect(after).toEqual([]);
    expect(refusal).toEqual({ name: "scan.pdf", code: "attachment_no_text", message: "scan.pdf has no text to read." });
  });

  it("refuses text that takes the message over the workspace total", () => {
    const drafts = [staged("a1", "r.pdf", PDF)];
    const limits = { ...DEFAULT_ATTACHMENT_LIMITS, max_total_bytes: 4_000 };
    const { drafts: after, refusal } = applyExtracted(drafts, answer({}), limits);
    expect(after).toEqual([]);
    expect(refusal?.code).toBe("attachment_total_too_large");
  });

  it("ignores an answer for a draft no longer staged", () => {
    const drafts = [staged("a2", "r.pdf", PDF)];
    const { drafts: after, refusal } = applyExtracted(drafts, answer({ request_id: "a1" }), DEFAULT_ATTACHMENT_LIMITS);
    expect(after).toEqual(drafts);
    expect(refusal).toBeNull();
  });
});

describe("formatTokens", () => {
  it("rounds for a chip", () => {
    expect(formatTokens(0)).toBe("≈0 tokens");
    expect(formatTokens(999)).toBe("≈999 tokens");
    expect(formatTokens(1250)).toBe("≈1.2k tokens");
    expect(formatTokens(2_400_000)).toBe("≈2.4M tokens");
  });
});

describe("a dropped connection (TD-5161)", () => {
  it("turns a chip still reading into its size, so it never spins forever", () => {
    const drafts = [staged("a1", "r.pdf", PDF)];
    const fallen = unmeasurePending(drafts);
    expect(fallen).not.toBeNull();
    expect(fallen?.[0].measure).toEqual({ state: "unmeasured" });
    expect(chipDetail(toChips(fallen ?? [])[0])).not.toContain("reading");
    expect(unmeasurePending(fallen ?? [])).toBeNull();
  });
});
