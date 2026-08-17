"""Text-file attachments on a user message (TD-1709).

**The daemon is the gate, not the composer.**  A client is not trustworthy:
it can be an older build, a stalled one, or something that is not our UI at
all.  So the caps and the text/binary test live here, on the receiving side,
and the composer's early refusal is a courtesy that buys better copy — never
the thing that actually holds the wall.

Attachments arrive as base64 so the daemon sees the *bytes*, not a decode a
client already made on our behalf.  That is what makes "is this text?"
answerable at all: a client that reads a PNG and ships it raw fails the
strict UTF-8 decode here, where a pre-decoded string would have arrived as
replacement characters and looked like ordinary prose.

Deliberately leaf: this module imports nothing from ``tstd``, so both
``protocol`` (the wire) and ``boundary_config`` (the caps) can depend on it
without a cycle.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

# Defaults when `.tst/config.yaml` declares no `attachments:` section.  Sized
# against the websocket frame ceiling (1 MiB, the `websockets` default): the
# total cap plus base64's third again has to fit inside one frame, or an
# oversize message would be dropped by the transport before this module ever
# got the chance to refuse it with copy the user can act on.
DEFAULT_MAX_FILE_BYTES = 256_000
DEFAULT_MAX_TOTAL_BYTES = 512_000
DEFAULT_MAX_COUNT = 10


class AttachmentLimits(BaseModel):
    """Caps on what one message may carry (`.tst/config.yaml` ``attachments``).

    Also ridden out on ``boundary_update`` so the composer refuses against
    the workspace's real numbers rather than a hardcoded guess (§6: the UI
    never derives truth it wasn't given).
    """

    max_file_bytes: int = Field(default=DEFAULT_MAX_FILE_BYTES, ge=1)
    max_total_bytes: int = Field(default=DEFAULT_MAX_TOTAL_BYTES, ge=1)
    max_count: int = Field(default=DEFAULT_MAX_COUNT, ge=1)


class AttachmentError(Exception):
    """A refused attachment, carrying the wire error code and its copy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AttachmentLike(Protocol):
    """The shape this module needs off the wire message.

    Structural rather than a concrete import: ``protocol.Attachment`` is the
    mirrored wire definition and must stay readable in ``protocol.py``, which
    already imports from here for the limits.
    """

    @property
    def name(self) -> str: ...

    @property
    def content_b64(self) -> str: ...


@dataclass(frozen=True)
class DecodedAttachment:
    """One attachment the daemon has accepted, decoded and measured itself."""

    name: str
    text: str
    size: int


def format_bytes(count: int) -> str:
    """Sizes for humans, in the copy a refusal shows."""
    if count < 1000:
        return f"{count} bytes"
    if count < 1_000_000:
        return f"{count / 1000:.1f} KB"
    return f"{count / 1_000_000:.1f} MB"


def _safe_name(raw: str) -> str:
    """The basename, with anything that could forge a delimiter refused.

    The name is client-supplied and lands in two places that matter: the
    prompt the model reads, and the chip the user reads.  A newline in it
    could counterfeit the block markers below, and a path could imply the
    daemon read a file it never touched — so both are stripped or refused
    rather than trusted.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in ("", ".", ".."):
        raise AttachmentError(
            "attachment_bad_name",
            f"An attachment arrived with an unusable file name ({raw!r}). "
            "Re-attach the file. Nothing was sent.",
        )
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise AttachmentError(
            "attachment_bad_name",
            f"The file name {name!r} contains control characters and was refused. "
            "Rename the file and re-attach it. Nothing was sent.",
        )
    return name


def _decode_text(name: str, raw: bytes) -> str:
    """Strict UTF-8, plus a NUL scan — the whole text/binary test.

    NUL is legal UTF-8 but is the oldest and most reliable binary tell, and
    it is what would end a C string mid-prompt; refusing it costs nothing on
    real text files.
    """
    binary = AttachmentError(
        "attachment_binary",
        f"{name} isn't a text file, so it can't be attached. TST Desk attaches "
        "text files only — images need vision support, which depends on the "
        "models you've chosen and isn't in this version. Nothing was sent.",
    )
    if b"\x00" in raw:
        raise binary
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise binary from e


def decode_attachments(
    items: Sequence[AttachmentLike],
    limits: AttachmentLimits,
) -> list[DecodedAttachment]:
    """Decode and vet every attachment on a message, or refuse the message.

    All-or-nothing on purpose: a partial send would drop a file the user can
    see in their own composer, and they would have no way to tell which.

    Raises:
        AttachmentError: On the first attachment that breaks a cap, fails to
            decode, or is not text.  ``code`` is the wire error code and
            ``message`` is copy that names the file and the way forward.
    """
    if not items:
        return []

    if len(items) > limits.max_count:
        raise AttachmentError(
            "attachment_too_many",
            f"{len(items)} files attached; this workspace allows "
            f"{limits.max_count} per message. Send fewer, or raise max_count "
            "under attachments: in .tst/config.yaml. Nothing was sent.",
        )

    decoded: list[DecodedAttachment] = []
    total = 0
    for item in items:
        name = _safe_name(item.name)
        try:
            raw = base64.b64decode(item.content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise AttachmentError(
                "attachment_invalid_encoding",
                f"{name} arrived in a form the daemon couldn't read. "
                "Re-attach it. Nothing was sent.",
            ) from e

        if len(raw) > limits.max_file_bytes:
            raise AttachmentError(
                "attachment_too_large",
                f"{name} is {format_bytes(len(raw))}; this workspace allows "
                f"{format_bytes(limits.max_file_bytes)} per file. Attach a "
                "smaller file, or raise max_file_bytes under attachments: in "
                ".tst/config.yaml. Nothing was sent.",
            )

        total += len(raw)
        if total > limits.max_total_bytes:
            raise AttachmentError(
                "attachment_total_too_large",
                f"The attachments total more than "
                f"{format_bytes(limits.max_total_bytes)}, which is this "
                "workspace's limit for one message. Send them across separate "
                "messages, or raise max_total_bytes under attachments: in "
                ".tst/config.yaml. Nothing was sent.",
            )

        decoded.append(DecodedAttachment(name=name, text=_decode_text(name, raw), size=len(raw)))

    return decoded


def render_user_content(text: str, attachments: Sequence[DecodedAttachment]) -> str:
    """Fold accepted attachments into the message the model actually reads.

    Plain delimiter lines rather than code fences: a fence would need
    escaping the moment an attached markdown file contained one, and the
    escaping is the part that goes wrong.  ``_safe_name`` is what keeps a
    file name from counterfeiting these lines.
    """
    if not attachments:
        return text

    blocks = [text] if text else []
    for item in attachments:
        blocks.append(
            f"--- attached file: {item.name} ({item.size} bytes) ---\n"
            f"{item.text}\n"
            f"--- end of {item.name} ---"
        )
    return "\n\n".join(blocks)
