"""Attachments v1 — the daemon's gate (TD-1709).

The point of this file is that the wall is on the *receiving* side.  Every
refusal below is driven through ``Daemon._handle_message`` with a payload a
well-behaved composer would never build, because that is exactly the case a
UI-only check cannot cover: an older client, a stalled one, or something
that is not our UI at all.  The composer's early refusal is a courtesy for
better copy, and it is tested separately on the TypeScript side.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from tstd.attachments import (
    DEFAULT_MAX_COUNT,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    AttachmentError,
    AttachmentLimits,
    DecodedAttachment,
    build_provider_user_content,
    decode_attachments,
    render_user_content,
)
from tstd.boundary_config import BoundaryConfig, load_workspace_boundary
from tstd.daemon import Daemon
from tstd.protocol import Attachment, BoundaryUpdate, UserMessage, parse_client_message
from tstd.session import QueuedUserMessage

# A real PNG header: valid bytes, invalid UTF-8, and what a user actually
# drags in when they try to attach a screenshot.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x06"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def attach(name: str, data: bytes) -> dict[str, str]:
    return {"name": name, "content_b64": b64(data)}


# ── The pure module ─────────────────────────────────────────────────────


class TestDecodeAttachments:
    def test_plain_text_round_trips(self) -> None:
        items = [Attachment(name="notes.md", content_b64=b64(b"# Title\n\nbody\n"))]
        decoded = decode_attachments(items, AttachmentLimits())
        assert decoded == [DecodedAttachment(name="notes.md", text="# Title\n\nbody\n", size=14)]

    def test_empty_file_is_legal_text(self) -> None:
        decoded = decode_attachments([Attachment(name="empty.txt")], AttachmentLimits())
        assert decoded == [DecodedAttachment(name="empty.txt", text="", size=0)]

    def test_no_attachments_is_not_an_error(self) -> None:
        assert decode_attachments([], AttachmentLimits()) == []

    def test_utf8_beyond_ascii_survives(self) -> None:
        text = "café — naïve — 日本語\n"
        decoded = decode_attachments(
            [Attachment(name="i18n.txt", content_b64=b64(text.encode("utf-8")))],
            AttachmentLimits(),
        )
        assert decoded[0].text == text

    def test_png_is_accepted_as_an_image(self) -> None:
        decoded = decode_attachments(
            [Attachment(name="shot.png", content_b64=b64(PNG_BYTES))],
            AttachmentLimits(),
            allow_images=True,
        )
        assert decoded[0].name == "shot.png"
        assert decoded[0].media_type == "image/png"
        assert decoded[0].data == PNG_BYTES
        assert decoded[0].text == ""

    def test_binary_is_refused(self) -> None:
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="blob.dat", content_b64=b64(b"\x00\x01\x02\x03"))],
                AttachmentLimits(),
            )
        assert excinfo.value.code == "attachment_binary"
        assert "blob.dat" in excinfo.value.message

    def test_image_accepted_when_vision_enabled(self) -> None:
        decoded = decode_attachments(
            [Attachment(name="shot.png", content_b64=b64(PNG_BYTES))],
            AttachmentLimits(),
            allow_images=True,
        )
        assert decoded[0].is_image
        assert decoded[0].mime == "image/png"
        assert decoded[0].data_b64 is not None

    def test_image_refused_when_vision_disabled(self) -> None:
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="shot.png", content_b64=b64(PNG_BYTES))],
                AttachmentLimits(),
                allow_images=False,
            )
        assert excinfo.value.code == "attachment_no_vision"

    def test_nul_byte_is_binary_even_when_utf8_decodable(self) -> None:
        """NUL is legal UTF-8 and still the oldest reliable binary tell."""
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="blob.dat", content_b64=b64(b"text\x00more"))],
                AttachmentLimits(),
            )
        assert excinfo.value.code == "attachment_binary"

    def test_utf16_is_refused_as_binary(self) -> None:
        """A UTF-16 file is 'text' to a human and not text on this wire."""
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="wide.txt", content_b64=b64("hello".encode("utf-16")))],
                AttachmentLimits(),
            )
        assert excinfo.value.code == "attachment_binary"

    def test_oversize_file_is_refused(self) -> None:
        limits = AttachmentLimits(max_file_bytes=10)
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments([Attachment(name="big.txt", content_b64=b64(b"x" * 11))], limits)
        assert excinfo.value.code == "attachment_too_large"
        assert "max_file_bytes" in excinfo.value.message

    def test_file_exactly_at_the_cap_is_accepted(self) -> None:
        limits = AttachmentLimits(max_file_bytes=10, max_total_bytes=10)
        decoded = decode_attachments(
            [Attachment(name="ten.txt", content_b64=b64(b"x" * 10))], limits
        )
        assert decoded[0].size == 10

    def test_total_across_files_is_refused(self) -> None:
        limits = AttachmentLimits(max_file_bytes=10, max_total_bytes=15)
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [
                    Attachment(name="a.txt", content_b64=b64(b"x" * 10)),
                    Attachment(name="b.txt", content_b64=b64(b"y" * 10)),
                ],
                limits,
            )
        assert excinfo.value.code == "attachment_total_too_large"
        assert "max_total_bytes" in excinfo.value.message

    def test_too_many_files_is_refused(self) -> None:
        limits = AttachmentLimits(max_count=2)
        items = [Attachment(name=f"f{i}.txt", content_b64=b64(b"x")) for i in range(3)]
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(items, limits)
        assert excinfo.value.code == "attachment_too_many"
        assert "max_count" in excinfo.value.message

    def test_bad_base64_is_refused(self) -> None:
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="x.txt", content_b64="not base64!!")], AttachmentLimits()
            )
        assert excinfo.value.code == "attachment_invalid_encoding"

    def test_directory_components_are_stripped_from_the_name(self) -> None:
        """The name is client-supplied and lands in the prompt and the chip."""
        decoded = decode_attachments(
            [Attachment(name="../../etc/passwd", content_b64=b64(b"root\n"))],
            AttachmentLimits(),
        )
        assert decoded[0].name == "passwd"

    def test_windows_separators_are_stripped_too(self) -> None:
        decoded = decode_attachments(
            [Attachment(name=r"C:\Users\me\notes.txt", content_b64=b64(b"hi"))],
            AttachmentLimits(),
        )
        assert decoded[0].name == "notes.txt"

    def test_a_name_that_is_only_a_path_is_refused(self) -> None:
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="dir/", content_b64=b64(b"hi"))], AttachmentLimits()
            )
        assert excinfo.value.code == "attachment_bad_name"

    def test_control_characters_in_a_name_are_refused(self) -> None:
        """A newline in the name could counterfeit the block delimiter."""
        with pytest.raises(AttachmentError) as excinfo:
            decode_attachments(
                [Attachment(name="a\n--- end of a ---", content_b64=b64(b"hi"))],
                AttachmentLimits(),
            )
        assert excinfo.value.code == "attachment_bad_name"

    def test_refusal_is_all_or_nothing(self) -> None:
        """A partial send would drop a file the user can still see in the composer."""
        items = [
            Attachment(name="good.txt", content_b64=b64(b"fine")),
            Attachment(name="bad.dat", content_b64=b64(b"\x00\x01\x02")),
        ]
        with pytest.raises(AttachmentError):
            decode_attachments(items, AttachmentLimits())

    def test_every_refusal_says_nothing_was_sent(self) -> None:
        """The message is dropped whole, so the copy must not imply otherwise."""
        cases: list[list[Attachment]] = [
            [Attachment(name="blob.dat", content_b64=b64(b"\x00\x01\x02"))],
            [Attachment(name="big.txt", content_b64=b64(b"x" * 20))],
            [Attachment(name="x.txt", content_b64="not base64!!")],
            [Attachment(name="dir/", content_b64=b64(b"hi"))],
        ]
        limits = AttachmentLimits(max_file_bytes=10, max_total_bytes=10, max_count=1)
        for items in cases:
            with pytest.raises(AttachmentError) as excinfo:
                decode_attachments(items, limits)
            assert "Nothing was sent." in excinfo.value.message


class TestBuildProviderUserContent:
    def test_text_only_stays_plain_string(self) -> None:
        decoded = [DecodedAttachment(name="a.txt", text="hi", size=2)]
        assert build_provider_user_content("hello", decoded) == (
            "hello\n\n--- attached file: a.txt (2 bytes) ---\nhi\n--- end of a.txt ---"
        )

    def test_image_becomes_multimodal_parts(self) -> None:
        item = DecodedAttachment(
            name="shot.png",
            size=len(PNG_BYTES),
            mime="image/png",
            data_b64=b64(PNG_BYTES),
        )
        parts = build_provider_user_content("look", [item])
        assert isinstance(parts, list)
        assert parts[0] == {"type": "text", "text": "look"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


class TestRenderUserContent:
    def test_no_attachments_leaves_the_text_alone(self) -> None:
        assert render_user_content("hello", []) == "hello"

    def test_attachment_is_delimited_and_named(self) -> None:
        rendered = render_user_content(
            "review this", [DecodedAttachment(name="a.py", text="print(1)", size=8)]
        )
        assert rendered == (
            "review this\n\n--- attached file: a.py (8 bytes) ---\nprint(1)\n--- end of a.py ---"
        )

    def test_attachment_only_message_has_no_leading_blank(self) -> None:
        rendered = render_user_content("", [DecodedAttachment(name="a.txt", text="x", size=1)])
        assert rendered.startswith("--- attached file: a.txt")

    def test_a_fence_inside_the_file_needs_no_escaping(self) -> None:
        body = "```python\nprint(1)\n```"
        rendered = render_user_content(
            "", [DecodedAttachment(name="a.md", text=body, size=len(body))]
        )
        assert body in rendered
        assert rendered.endswith("--- end of a.md ---")


# ── Config ──────────────────────────────────────────────────────────────


class TestAttachmentLimitsConfig:
    def test_absent_file_means_the_documented_defaults(self, tmp_path: Path) -> None:
        limits = load_workspace_boundary(tmp_path).attachments
        assert limits.max_file_bytes == DEFAULT_MAX_FILE_BYTES
        assert limits.max_total_bytes == DEFAULT_MAX_TOTAL_BYTES
        assert limits.max_count == DEFAULT_MAX_COUNT

    def test_workspace_may_tighten_the_caps(self, tmp_path: Path) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text(
            "attachments:\n  max_file_bytes: 1024\n  max_count: 2\n", encoding="utf-8"
        )
        limits = load_workspace_boundary(tmp_path).attachments
        assert limits.max_file_bytes == 1024
        assert limits.max_count == 2
        # Unstated keys keep their defaults rather than collapsing to zero.
        assert limits.max_total_bytes == DEFAULT_MAX_TOTAL_BYTES

    def test_the_scaffolded_template_still_round_trips_to_defaults(self, tmp_path: Path) -> None:
        """The template documents the new section without pinning it."""
        from tstd.boundary_config import scaffold_workspace_config

        scaffold_workspace_config(tmp_path)
        text = (tmp_path / ".tst" / "config.yaml").read_text(encoding="utf-8")
        assert "max_file_bytes" in text
        assert load_workspace_boundary(tmp_path) == BoundaryConfig()


# ── The wire ────────────────────────────────────────────────────────────


class TestUserMessageWire:
    def test_attachments_are_optional(self) -> None:
        """Additive field: a client that never sends one behaves as it did."""
        msg = parse_client_message(
            json.dumps({"type": "user_message", "session_id": "s", "content": "hi"})
        )
        assert isinstance(msg, UserMessage)
        assert msg.attachments == []

    def test_attachments_parse_off_the_wire(self) -> None:
        msg = parse_client_message(
            json.dumps(
                {
                    "type": "user_message",
                    "session_id": "s",
                    "content": "hi",
                    "attachments": [attach("a.txt", b"body")],
                }
            )
        )
        assert isinstance(msg, UserMessage)
        assert msg.attachments[0].name == "a.txt"


# ── The daemon refuses, whatever the client sends ───────────────────────


async def _open(daemon: Daemon, path: Path) -> str:
    raw = json.dumps({"type": "open_workspace", "path": str(path)})
    response = await daemon._handle_message(raw, None)
    assert response is not None
    session_id: str = json.loads(response)["session_id"]
    return session_id


async def _send(
    daemon: Daemon, session_id: str, attachments: list[dict[str, str]], content: str = "look"
) -> dict[str, Any] | None:
    raw = json.dumps(
        {
            "type": "user_message",
            "session_id": session_id,
            "content": content,
            "attachments": attachments,
        }
    )
    response = await daemon._handle_message(raw, None)
    return None if response is None else dict(json.loads(response))


class TestDaemonRefusal:
    """Nothing here goes through the composer — that is the whole point."""

    async def test_oversize_attachment_is_refused(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        oversize = b"x" * (DEFAULT_MAX_FILE_BYTES + 1)
        reply = await _send(daemon, session_id, [attach("huge.txt", oversize)])

        assert reply is not None
        assert reply["type"] == "error"
        assert reply["code"] == "attachment_too_large"
        assert reply["session_id"] == session_id
        assert "huge.txt" in reply["message"]
        assert sess._user_message_queue.empty()
        await daemon._shutdown()

    async def test_binary_attachment_is_refused(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        reply = await _send(daemon, session_id, [attach("blob.dat", b"\x00\x01\x02\x03")])

        assert reply is not None
        assert reply["code"] == "attachment_binary"
        assert "blob.dat" in reply["message"]
        assert sess._user_message_queue.empty()
        await daemon._shutdown()

    async def test_image_attachment_is_refused_without_vision(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        reply = await _send(daemon, session_id, [attach("screenshot.png", PNG_BYTES)])

        assert reply is not None
        assert reply["code"] == "attachment_no_vision"
        assert "screenshot.png" in reply["message"]
        assert sess._user_message_queue.empty()
        await daemon._shutdown()

    async def test_too_many_attachments_is_refused(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)

        items = [attach(f"f{i}.txt", b"x") for i in range(DEFAULT_MAX_COUNT + 1)]
        reply = await _send(daemon, session_id, items)

        assert reply is not None
        assert reply["code"] == "attachment_too_many"
        await daemon._shutdown()

    async def test_total_size_is_refused(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)

        # Each file is legal on its own; together they are not.
        assert DEFAULT_MAX_FILE_BYTES * 3 > DEFAULT_MAX_TOTAL_BYTES
        items = [attach(f"f{i}.txt", b"x" * DEFAULT_MAX_FILE_BYTES) for i in range(3)]
        reply = await _send(daemon, session_id, items)

        assert reply is not None
        assert reply["code"] == "attachment_total_too_large"
        await daemon._shutdown()

    async def test_the_workspace_cap_is_what_gates(self, tmp_path: Path) -> None:
        """A tightened `.tst/config.yaml` refuses what the default would take."""
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        (workspace / ".tst" / "config.yaml").write_text(
            "attachments:\n  max_file_bytes: 16\n", encoding="utf-8"
        )
        daemon = Daemon(data_dir=tmp_path / "data")
        session_id = await _open(daemon, workspace)

        reply = await _send(daemon, session_id, [attach("small.txt", b"x" * 17)])
        assert reply is not None
        assert reply["code"] == "attachment_too_large"
        assert "16 bytes" in reply["message"]
        await daemon._shutdown()

    async def test_accepted_attachment_reaches_the_loop_rendered(self, tmp_path: Path) -> None:
        """The model sees the file's text, delimited and named."""
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        # The session's loop drains the queue on a 50ms poll, so capture at
        # the enqueue rather than racing it.
        captured: list[QueuedUserMessage] = []
        original = sess.add_user_message

        async def spy(content: str | QueuedUserMessage) -> None:
            captured.append(
                QueuedUserMessage.plain(content) if isinstance(content, str) else content
            )
            await original(content)

        sess.add_user_message = spy  # type: ignore[method-assign]

        reply = await _send(
            daemon, session_id, [attach("hello.py", b"print('hi')\n")], content="explain this"
        )

        assert reply is None  # no error is the ack
        assert len(captured) == 1
        assert captured[0].display == (
            "explain this\n\n"
            "--- attached file: hello.py (12 bytes) ---\n"
            "print('hi')\n\n"
            "--- end of hello.py ---"
        )
        await daemon._shutdown()

    async def test_a_message_with_no_attachments_is_untouched(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        captured: list[QueuedUserMessage] = []
        original = sess.add_user_message

        async def spy(content: str | QueuedUserMessage) -> None:
            captured.append(
                QueuedUserMessage.plain(content) if isinstance(content, str) else content
            )
            await original(content)

        sess.add_user_message = spy  # type: ignore[method-assign]

        raw = json.dumps({"type": "user_message", "session_id": session_id, "content": "plain"})
        assert await daemon._handle_message(raw, None) is None
        assert len(captured) == 1
        assert captured[0].display == "plain"
        await daemon._shutdown()

    async def test_boundary_update_carries_the_limits(self, tmp_path: Path) -> None:
        """The composer refuses against the workspace's numbers, not a guess."""
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        (workspace / ".tst" / "config.yaml").write_text(
            "attachments:\n  max_count: 3\n", encoding="utf-8"
        )
        daemon = Daemon(data_dir=tmp_path / "data")
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        updates = [e for e in sess.event_log.all_events if isinstance(e, BoundaryUpdate)]
        assert len(updates) == 1
        assert updates[0].attachments.max_count == 3
        assert updates[0].attachments.max_file_bytes == DEFAULT_MAX_FILE_BYTES
        await daemon._shutdown()

    async def test_image_accepted_when_vision_enabled(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        preset_name = daemon.config.active_preset
        preset = daemon.config.presets[preset_name]
        brain = preset.brain.model_copy(update={"vision": True})
        daemon.config = daemon.config.model_copy(
            update={
                "presets": {
                    **daemon.config.presets,
                    preset_name: preset.model_copy(update={"brain": brain}),
                }
            }
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_id = await _open(daemon, workspace)
        sess = daemon.session_registry.get(session_id)
        assert sess is not None

        captured: list[QueuedUserMessage] = []
        original = sess.add_user_message

        async def spy(content: str | QueuedUserMessage) -> None:
            captured.append(
                QueuedUserMessage.plain(content) if isinstance(content, str) else content
            )
            await original(content)

        sess.add_user_message = spy  # type: ignore[method-assign]

        reply = await _send(daemon, session_id, [attach("shot.png", PNG_BYTES)], content="look")
        assert reply is None
        assert len(captured) == 1
        assert "[Images attached: shot.png" in captured[0].display
        assert isinstance(captured[0].provider, list)
        assert captured[0].provider[1]["type"] == "image_url"
        await daemon._shutdown()
