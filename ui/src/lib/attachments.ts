// Composer-side attachment handling (TD-1709).
//
// This is the *courtesy* half of the gate. The daemon refuses an oversize or
// binary attachment on arrival no matter what a client sends — see
// core/tstd/attachments.py — and nothing here weakens or replaces that. What
// it buys is copy: a file refused at the composer names itself the moment the
// user drops it, instead of after a round trip that also swallowed the
// message they typed alongside it.
//
// The two sides run the same test on purpose: strict UTF-8 plus a NUL scan,
// against the same caps. The caps arrive on `boundary_update` rather than
// being hardcoded here (§6: the UI never derives truth it wasn't given).
//
// Pure and DOM-free apart from TextDecoder (available in node and the
// browser), so it unit-tests under vitest's node environment.

import type { Attachment, AttachmentExtracted, AttachmentLimits, ExtractAttachment } from "./protocol";

/** Mirrors core/tstd/attachments.py. Used only until the first
 *  `boundary_update` names the workspace's real numbers. */
export const DEFAULT_ATTACHMENT_LIMITS: AttachmentLimits = {
  max_file_bytes: 4_000_000,
  max_total_bytes: 8_000_000,
  max_count: 10,
};

/** Mirrors core/tstd/attachments.py (TD-5131): the transport's ceilings on a
 *  document's file and on all the files of one message. Not workspace caps:
 *  past these the websocket drops the frame with no word at all. */
export const MAX_DOCUMENT_FILE_BYTES = 10_000_000;
export const MAX_MESSAGE_FILE_BYTES = 12_000_000;

export type DocumentKind = "pdf" | "docx" | "xlsx";

/** What the daemon said a staged document holds (TD-5131). `pending` until
 *  `attachment_extracted` answers; `unmeasured` when the ask could not go
 *  out, and the send will measure it instead. */
export type DocumentMeasure =
  | { state: "pending" }
  | { state: "unmeasured" }
  | { state: "ready"; units: number; text_bytes: number; tokens: number };

/** An accepted file, before the composer gives it a row id. */
export interface NewAttachment {
  name: string;
  /** Byte length of the file. The caps measure this, except for a document,
   *  whose caps measure its extracted text (TD-5131). */
  size: number;
  /** The file's bytes, base64'd for the wire. */
  content_b64: string;
  /** Set for a PDF, DOCX or XLSX, which is sent as its text (TD-5131). */
  document?: DocumentKind;
  measure?: DocumentMeasure;
}

/** One accepted file, held in the composer until the message goes out. */
export interface AttachmentDraft extends NewAttachment {
  /** Composer-local id; never a conversation message id. */
  id: string;
}

/** A file the composer would not take, and the sentence explaining why. */
export interface AttachmentRefusal {
  name: string;
  /** Matches the daemon's wire error code for the same condition. */
  code: string;
  message: string;
}

export type AttachmentOutcome =
  | { ok: true; draft: NewAttachment }
  | { ok: false; refusal: AttachmentRefusal };

/** Sizes for humans: the daemon's format_bytes (core/tstd/attachments.py)
 *  to the character, so a card and the tool result the model repeats agree. */
export function formatBytes(count: number): string {
  if (count < 1000) return `${count} bytes`;
  if (count < 1_000_000) return `${oneDecimal(count / 1000)} KB`;
  return `${oneDecimal(count / 1_000_000)} MB`;
}

/** Python's f"{x:.1f}" for a positive x: the nearest tenth, an exact tie to
 *  even. toFixed picks the nearest too but rounds an exact tie up. In binary
 *  the only exact ties are x.25 and x.75 (4x is an odd integer); x.75 goes
 *  up to an even tenth either way, so only x.25 needs turning down. */
function oneDecimal(x: number): string {
  const quarters = x * 4;
  if (Number.isSafeInteger(quarters) && quarters % 4 === 1) return `${Math.floor(x)}.2`;
  return x.toFixed(1);
}

/** What the caps count for one draft: a document's text once measured
 *  (nothing until then), any other file's bytes. */
export function countedBytes(draft: NewAttachment): number {
  if (draft.document === undefined) return draft.size;
  return draft.measure?.state === "ready" ? draft.measure.text_bytes : 0;
}

/** Bytes already staged, which the per-message total is measured against. */
export function totalBytes(drafts: readonly NewAttachment[]): number {
  return drafts.reduce((sum, d) => sum + countedBytes(d), 0);
}

/** File bytes staged, against the transport's ceiling for one message. */
export function wireBytes(drafts: readonly NewAttachment[]): number {
  return drafts.reduce((sum, d) => sum + d.size, 0);
}

/** PDF from its header; DOCX and XLSX are zips, told apart by the name here.
 *  Only a hint for the chip: the daemon opens the zip and decides. */
export function detectDocument(name: string, bytes: Uint8Array): DocumentKind | null {
  if (bytes.length >= 5 && String.fromCharCode(...bytes.subarray(0, 5)) === "%PDF-") return "pdf";
  const zip = bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 3 && bytes[3] === 4;
  if (!zip) return null;
  const lower = name.toLowerCase();
  if (lower.endsWith(".docx")) return "docx";
  if (lower.endsWith(".xlsx")) return "xlsx";
  return null;
}

/** Basename only. The daemon strips this again — it does not trust us — but
 *  a chip reading `passwd` rather than `../../etc/passwd` is the honest
 *  preview of what will actually be sent. */
export function safeName(raw: string): string {
  return raw.replace(/\\/g, "/").split("/").pop()?.trim() ?? "";
}

/** Strict UTF-8 plus a NUL scan: the whole text/binary test, same as the
 *  daemon's. `fatal` is what makes it a test rather than a lossy decode —
 *  without it a PNG comes back as replacement characters and reads as prose. */
export function isImageBytes(bytes: Uint8Array): boolean {
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47
  ) {
    return true;
  }
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return true;
  }
  if (bytes.length >= 6) {
    const ascii = String.fromCharCode(...bytes.subarray(0, 6));
    if (ascii === "GIF87a" || ascii === "GIF89a") return true;
  }
  if (
    bytes.length >= 12 &&
    String.fromCharCode(...bytes.subarray(0, 4)) === "RIFF" &&
    String.fromCharCode(...bytes.subarray(8, 12)) === "WEBP"
  ) {
    return true;
  }
  return false;
}

export function isTextBytes(bytes: Uint8Array): boolean {
  if (bytes.includes(0)) return false;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return true;
  } catch {
    return false;
  }
}

/** Base64 without a data-URL round trip; chunked so a large file cannot blow
 *  the argument limit on String.fromCharCode. */
export function toBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/** Magic-byte image detection — same set as core/tstd/attachments.py (TD-4705). */
export function detectImageMime(bytes: Uint8Array): string | null {
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return "image/png";
  }
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 6 &&
    ((bytes[0] === 0x47 &&
      bytes[1] === 0x49 &&
      bytes[2] === 0x46 &&
      bytes[3] === 0x38 &&
      (bytes[4] === 0x37 || bytes[4] === 0x39) &&
      bytes[5] === 0x61) ||
      (bytes[0] === 0x47 &&
        bytes[1] === 0x49 &&
        bytes[2] === 0x46 &&
        bytes[3] === 0x38 &&
        bytes[4] === 0x39 &&
        bytes[5] === 0x61))
  ) {
    return "image/gif";
  }
  if (
    bytes.length >= 12 &&
    bytes[0] === 0x52 &&
    bytes[1] === 0x49 &&
    bytes[2] === 0x46 &&
    bytes[3] === 0x46 &&
    bytes[8] === 0x57 &&
    bytes[9] === 0x45 &&
    bytes[10] === 0x42 &&
    bytes[11] === 0x50
  ) {
    return "image/webp";
  }
  return null;
}

/** Vet one dropped/picked/pasted file against the caps and the text test.
 *
 *  `staged` is what the composer already holds, so the count and the running
 *  total are judged against the message as it will actually be sent. Every
 *  refusal names the file and the way forward, and none of them claims
 *  anything was sent — nothing has been.
 *
 *  When `allowImages` is true (from `tier_state.vision`, TD-4705), PNG/JPEG/
 *  GIF/WebP pass; other binary still refuses. When false, images refuse with
 *  `attachment_no_vision` rather than the generic binary copy. */
export function acceptAttachment(
  rawName: string,
  bytes: Uint8Array,
  limits: AttachmentLimits,
  staged: readonly NewAttachment[] = [],
  allowImages = false,
): AttachmentOutcome {
  const name = safeName(rawName) || "file";

  if (staged.length >= limits.max_count) {
    return {
      ok: false,
      refusal: {
        name,
        code: "attachment_too_many",
        message: `You can attach ${limits.max_count} file${limits.max_count === 1 ? "" : "s"} to one message. Send these first, or raise max_count under attachments: in .tst/config.yaml.`,
      },
    };
  }

  const document = detectDocument(name, bytes);
  if (document !== null) {
    if (bytes.length > MAX_DOCUMENT_FILE_BYTES) {
      return {
        ok: false,
        refusal: {
          name,
          code: "attachment_too_large",
          message: `${name} is ${formatBytes(bytes.length)}; a document can be at most ${formatBytes(MAX_DOCUMENT_FILE_BYTES)} to attach. Split it, or save its text as a text file and attach that.`,
        },
      };
    }
    if (wireBytes(staged) + bytes.length > MAX_MESSAGE_FILE_BYTES) {
      return {
        ok: false,
        refusal: {
          name,
          code: "attachment_total_too_large",
          message: `Adding ${name} would put this message over ${formatBytes(MAX_MESSAGE_FILE_BYTES)} of files, the most one message can carry. Send what's here first.`,
        },
      };
    }
    return {
      ok: true,
      draft: {
        name,
        size: bytes.length,
        content_b64: toBase64(bytes),
        document,
        measure: { state: "pending" },
      },
    };
  }

  if (bytes.length > limits.max_file_bytes) {
    return {
      ok: false,
      refusal: {
        name,
        code: "attachment_too_large",
        message: `${name} is ${formatBytes(bytes.length)}, and the limit is ${formatBytes(limits.max_file_bytes)} per file. Attach a smaller file, or raise max_file_bytes under attachments: in .tst/config.yaml.`,
      },
    };
  }

  if (totalBytes(staged) + bytes.length > limits.max_total_bytes) {
    return {
      ok: false,
      refusal: {
        name,
        code: "attachment_total_too_large",
        message: `Adding ${name} would put this message over ${formatBytes(limits.max_total_bytes)} of attachments. Send what's here first, or raise max_total_bytes under attachments: in .tst/config.yaml.`,
      },
    };
  }

  if (wireBytes(staged) + bytes.length > MAX_MESSAGE_FILE_BYTES) {
    return {
      ok: false,
      refusal: {
        name,
        code: "attachment_total_too_large",
        message: `Adding ${name} would put this message over ${formatBytes(MAX_MESSAGE_FILE_BYTES)} of files, the most one message can carry. Send what's here first.`,
      },
    };
  }

  const mime = detectImageMime(bytes);
  if (mime !== null) {
    if (!allowImages) {
      return {
        ok: false,
        refusal: {
          name,
          code: "attachment_no_vision",
          message: `${name} is an image, but the active model does not accept images. Set vision: true on that tier in config.yaml, or switch to a vision-capable preset.`,
        },
      };
    }
    return {
      ok: true,
      draft: { name, size: bytes.length, content_b64: toBase64(bytes) },
    };
  }

  if (!isTextBytes(bytes)) {
    return {
      ok: false,
      refusal: {
        name,
        code: "attachment_binary",
        message: `${name} isn't a text file, a PDF, DOCX or XLSX document, or a PNG/JPEG/GIF/WebP image, so it can't be attached.`,
      },
    };
  }

  return {
    ok: true,
    draft: { name, size: bytes.length, content_b64: toBase64(bytes) },
  };
}

/** The wire shape, stripped of the composer-local bookkeeping. */
export function toWireAttachments(drafts: readonly NewAttachment[]): Attachment[] {
  return drafts.map((d) => ({ name: d.name, content_b64: d.content_b64 }));
}

/** What a sent row shows: the chip's label, without the bytes riding along in
 *  the transcript for the life of the session. */
export interface AttachmentChip {
  name: string;
  size: number;
  /** A document sent as its text (TD-5131), and what the daemon measured. */
  document?: DocumentKind;
  measure?: DocumentMeasure;
}

export function toChips(drafts: readonly NewAttachment[]): AttachmentChip[] {
  return drafts.map((d) =>
    d.document === undefined
      ? { name: d.name, size: d.size }
      : { name: d.name, size: d.size, document: d.document, measure: d.measure ?? { state: "pending" } },
  );
}

const KIND_LABEL: Record<DocumentKind, string> = { pdf: "PDF", docx: "DOCX", xlsx: "XLSX" };

/** "≈1.2k tokens": the daemon's estimate, rounded for a chip. */
export function formatTokens(tokens: number): string {
  if (tokens < 1000) return `≈${tokens} token${tokens === 1 ? "" : "s"}`;
  if (tokens < 1_000_000) return `≈${oneDecimal(tokens / 1000)}k tokens`;
  return `≈${oneDecimal(tokens / 1_000_000)}M tokens`;
}

/** The chip's small print: a file's size, or a document's kind, pages or
 *  sheets and token count (TD-5131). */
export function chipDetail(chip: AttachmentChip): string {
  if (chip.document === undefined) return formatBytes(chip.size);
  const parts = [KIND_LABEL[chip.document]];
  const m = chip.measure ?? { state: "pending" };
  if (m.state === "pending") return `${parts[0]} · reading…`;
  if (m.state === "unmeasured") return `${parts[0]} · ${formatBytes(chip.size)}`;
  if (chip.document === "pdf") parts.push(`${m.units} page${m.units === 1 ? "" : "s"}`);
  if (chip.document === "xlsx") parts.push(`${m.units} sheet${m.units === 1 ? "" : "s"}`);
  parts.push(formatTokens(m.tokens));
  return parts.join(" · ");
}

/** The ask that measures a staged document; `request_id` is the draft's id. */
export function extractRequest(sessionId: string, draft: AttachmentDraft): ExtractAttachment {
  return {
    type: "extract_attachment",
    session_id: sessionId,
    request_id: draft.id,
    attachment: { name: draft.name, content_b64: draft.content_b64 },
  };
}

/** Fold the daemon's answer into the staged drafts. A refusal, or text that
 *  takes the message over the workspace's total, drops that document and
 *  names why, as a refusal at staging would have. An answer for a draft no
 *  longer staged changes nothing. */
export function applyExtracted(
  drafts: readonly AttachmentDraft[],
  event: AttachmentExtracted,
  limits: AttachmentLimits,
): { drafts: AttachmentDraft[]; refusal: AttachmentRefusal | null } {
  const index = drafts.findIndex((d) => d.id === event.request_id && d.document !== undefined);
  if (index < 0) return { drafts: [...drafts], refusal: null };
  const draft = drafts[index];
  const others = drafts.filter((_, i) => i !== index);
  if (event.error_code !== null) {
    return {
      drafts: others,
      refusal: { name: draft.name, code: event.error_code, message: event.message },
    };
  }
  if (totalBytes(others) + event.text_bytes > limits.max_total_bytes) {
    return {
      drafts: others,
      refusal: {
        name: draft.name,
        code: "attachment_total_too_large",
        message: `The text in ${draft.name} would put this message over ${formatBytes(limits.max_total_bytes)} of attachments. Send what's here first, or raise max_total_bytes under attachments: in .tst/config.yaml.`,
      },
    };
  }
  const measured: AttachmentDraft = {
    ...draft,
    measure: { state: "ready", units: event.units, text_bytes: event.text_bytes, tokens: event.tokens },
  };
  return { drafts: drafts.map((d, i) => (i === index ? measured : d)), refusal: null };
}
