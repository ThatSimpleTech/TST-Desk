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

import type { Attachment, AttachmentLimits } from "./protocol";

/** Mirrors core/tstd/attachments.py. Used only until the first
 *  `boundary_update` names the workspace's real numbers. */
export const DEFAULT_ATTACHMENT_LIMITS: AttachmentLimits = {
  max_file_bytes: 256_000,
  max_total_bytes: 512_000,
  max_count: 10,
};

/** An accepted file, before the composer gives it a row id. */
export interface NewAttachment {
  name: string;
  /** Byte length of the file, which is what the caps are measured against. */
  size: number;
  /** The file's bytes, base64'd for the wire. */
  content_b64: string;
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

/** Sizes for humans — the same shape the daemon's refusals use. */
export function formatBytes(count: number): string {
  if (count < 1000) return `${count} bytes`;
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)} KB`;
  return `${(count / 1_000_000).toFixed(1)} MB`;
}

/** Bytes already staged, which the per-message total is measured against. */
export function totalBytes(drafts: readonly NewAttachment[]): number {
  return drafts.reduce((sum, d) => sum + d.size, 0);
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
        message: `${name} isn't a text file or a PNG/JPEG/GIF/WebP image, so it can't be attached.`,
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
}

export function toChips(drafts: readonly NewAttachment[]): AttachmentChip[] {
  return drafts.map((d) => ({ name: d.name, size: d.size }));
}
