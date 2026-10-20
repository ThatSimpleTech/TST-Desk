"""PDF, DOCX and XLSX attachments arrive as their text (TD-5131).

Before this story every one of them was refused as ``attachment_binary``.
Now the daemon extracts the text on this machine, with page and sheet
markers, measures the caps against the text, and answers the composer's
``extract_attachment`` with a token count for the chip.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import socket
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tests.doc_fixtures import make_docx, make_pdf, make_xlsx, raw_xlsx
from tests.test_attachments import _open, _send, attach
from tstd.attachment_extract import DocumentError, extract_document, sniff_document
from tstd.attachment_office import MAX_XML_BYTES, MAX_XML_ELEMENTS
from tstd.attachments import (
    MAX_DOCUMENT_FILE_BYTES,
    AttachmentError,
    AttachmentLimits,
    decode_attachments,
    render_user_content,
)
from tstd.daemon import Daemon
from tstd.peer_policy import FORWARDED_VERBS, MACHINE_PEER_VERBS
from tstd.protocol import Attachment, parse_client_message
from tstd.session import QueuedUserMessage
from tstd.session_lifecycle import BUSY_CODE


def _item(name: str, data: bytes) -> Attachment:
    return Attachment(name=name, content_b64=base64.b64encode(data).decode("ascii"))


LIMITS = AttachmentLimits()


# ── Extraction, with page and sheet markers ────────────────────────────


def test_pdf_text_comes_out_page_by_page_with_markers() -> None:
    pdf = make_pdf(["Quarterly summary", "Revenue (up) 12%"])
    [doc] = decode_attachments([_item("report.pdf", pdf)], LIMITS)
    assert doc.document == "pdf"
    assert doc.units == 2
    assert doc.text == "[page 1 of 2]\nQuarterly summary\n[page 2 of 2]\nRevenue (up) 12%"
    assert not doc.is_image


def test_docx_paragraphs_and_table_rows() -> None:
    docx = make_docx(["Meeting notes", "Ship & tell"], [["Owner", "Task"], ["Ana", "Docs"]])
    [doc] = decode_attachments([_item("notes.docx", docx)], LIMITS)
    assert doc.document == "docx"
    assert doc.text == "Meeting notes\nShip & tell\nOwner\tTask\nAna\tDocs"


def test_xlsx_sheets_in_workbook_order_with_columns_kept() -> None:
    xlsx = make_xlsx(
        {
            "Totals": [["Item", "", "Qty"], ["apples", "", 3]],
            "Notes": [["remember the pears"]],
        }
    )
    [doc] = decode_attachments([_item("stock.xlsx", xlsx)], LIMITS)
    assert doc.document == "xlsx"
    assert doc.units == 2
    # make_xlsx stores the sheets backwards; the names still match their rows.
    assert doc.text == (
        "[sheet: Totals]\nItem\t\tQty\napples\t\t3\n[sheet: Notes]\nremember the pears"
    )


def test_the_model_reads_the_text_under_a_header_naming_the_document() -> None:
    pdf = make_pdf(["One page only"])
    decoded = decode_attachments([_item("a.pdf", pdf)], LIMITS)
    body = render_user_content("summarise", decoded)
    assert body == (
        "summarise\n\n"
        "--- attached file: a.pdf (PDF, 1 page, text extracted locally) ---\n"
        "[page 1 of 1]\nOne page only\n"
        "--- end of a.pdf ---"
    )


def test_kind_comes_from_the_bytes_not_the_name() -> None:
    assert sniff_document(make_pdf(["x"])) == "pdf"
    assert sniff_document(make_docx(["x"])) == "docx"
    assert sniff_document(make_xlsx({"S": [["x"]]})) == "xlsx"
    # A text file that only mentions a PDF header stays a text file.
    note = b"How to spot a PDF: it starts with %PDF-1.7\n"
    assert sniff_document(note) is None
    [doc] = decode_attachments([_item("note.pdf", note)], LIMITS)
    assert doc.document is None and doc.text == note.decode()
    # Any other zip is still binary.
    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("a.docx", other.getvalue())], LIMITS)
    assert excinfo.value.code == "attachment_binary"


# ── Caps measure the text, not the file ────────────────────────────────


def test_a_big_file_with_little_text_fits() -> None:
    pdf = make_pdf(["short"], padding=20_000)
    limits = AttachmentLimits(max_file_bytes=1_000, max_total_bytes=1_000)
    assert len(pdf) > limits.max_file_bytes
    [doc] = decode_attachments([_item("big.pdf", pdf)], limits)
    assert doc.size == len(pdf)
    assert doc.counted_bytes == len(doc.text.encode()) < 1_000


def test_a_small_file_with_too_much_text_is_refused() -> None:
    docx = make_docx(["word " * 400] * 10)  # ~20 KB of text, ~1 KB zipped
    limits = AttachmentLimits(max_file_bytes=5_000)
    assert len(docx) < limits.max_file_bytes
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("long.docx", docx)], limits)
    assert excinfo.value.code == "attachment_too_large"
    assert "text" in excinfo.value.message


def test_the_message_total_counts_document_text() -> None:
    xlsx = make_xlsx({"S": [["x" * 600]]})
    limits = AttachmentLimits(max_file_bytes=1_000, max_total_bytes=1_000)
    decode_attachments([_item("one.xlsx", xlsx)], limits)
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("one.xlsx", xlsx), _item("two.xlsx", xlsx)], limits)
    assert excinfo.value.code == "attachment_total_too_large"


def test_a_document_file_past_the_transport_ceiling_is_refused() -> None:
    pdf = make_pdf(["x"], padding=MAX_DOCUMENT_FILE_BYTES)
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("huge.pdf", pdf)], LIMITS)
    assert excinfo.value.code == "attachment_too_large"


# ── Hostile and broken files ───────────────────────────────────────────


def test_a_pdf_without_text_says_so() -> None:
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("scan.pdf", make_pdf(["", ""]))], LIMITS)
    assert excinfo.value.code == "attachment_no_text"


def test_a_damaged_pdf_is_unreadable_not_a_crash() -> None:
    with pytest.raises(AttachmentError) as excinfo:
        decode_attachments([_item("bad.pdf", b"%PDF-1.4\ngarbage")], LIMITS)
    assert excinfo.value.code == "attachment_unreadable"


def test_entity_expansion_is_refused() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        "<w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("word/document.xml", bomb)
    with pytest.raises(DocumentError) as excinfo:
        extract_document("x.docx", "docx", out.getvalue(), max_text_bytes=10_000)
    assert excinfo.value.code == "attachment_unreadable"


def test_a_zip_bomb_stops_at_the_member_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tstd.attachment_office.MAX_XML_BYTES", 10_000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "<a>" + " " * 1_000_000 + "</a>")
    assert len(out.getvalue()) < 10_000 < 1_000_000
    with pytest.raises(DocumentError) as excinfo:
        extract_document("x.docx", "docx", out.getvalue(), max_text_bytes=10_000)
    assert excinfo.value.code == "attachment_too_large"
    assert MAX_XML_BYTES == 64 * 1024 * 1024


def _extract_xlsx(raw: bytes, cap: int = 4_000_000) -> str:
    try:
        return extract_document("x.xlsx", "xlsx", raw, max_text_bytes=cap).text
    except DocumentError as e:
        return e.code


def test_a_sheet_of_empty_rows_stops_at_the_element_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty rows add nothing to the text, so the text cap never stops them;
    # before the budget every one was parsed (minutes, gigabytes at scale).
    monkeypatch.setattr("tstd.attachment_office.MAX_XML_ELEMENTS", 10_000)
    assert (
        _extract_xlsx(
            raw_xlsx(["<row/>" * 9_000 + '<row><c t="inlineStr"><is><t>x</t></is></c></row>'])
        )
        == "[sheet: S1]\nx"
    )
    assert _extract_xlsx(raw_xlsx(["<row/>" * 20_000])) == "attachment_too_large"
    # The budget is the whole document's, not each sheet's.
    assert _extract_xlsx(raw_xlsx(["<row/>" * 4_000] * 3)) == "attachment_too_large"
    assert MAX_XML_ELEMENTS == 4_000_000


def test_the_decompressed_budget_spans_every_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tstd.attachment_office.MAX_TOTAL_XML_BYTES", 100_000)
    one = "<row/>" * 6_000  # 36 KB of XML a sheet, under the per-member cap
    assert (
        _extract_xlsx(raw_xlsx([one + '<row><c t="inlineStr"><is><t>x</t></is></c></row>']))
        == "[sheet: S1]\nx"
    )
    assert _extract_xlsx(raw_xlsx([one] * 3)) == "attachment_too_large"


def test_a_big_part_is_streamed_not_held(monkeypatch: pytest.MonkeyPatch) -> None:
    # 300,000 empty rows: as one tree that is tens of MB of elements; read
    # as a stream it is little more than the XML bytes themselves.
    body = "<row/>" * 300_000
    raw = raw_xlsx([body])
    tracemalloc.start()
    try:
        assert _extract_xlsx(raw) == "attachment_no_text"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * len(body) + 4_000_000


def test_an_empty_far_right_cell_costs_nothing() -> None:
    # Was: pad to XFD and pop back, 16k list steps a row for no output.
    rows = '<row><c r="XFD1"/></row>' * 20_000
    started = time.monotonic()
    assert _extract_xlsx(raw_xlsx([rows])) == "attachment_no_text"
    assert time.monotonic() - started < 2.0
    # With text there, the blanks are kept so the column still lines up,
    # and they are tabs the cap counts.
    far = '<row><c r="A1" t="inlineStr"><is><t>a</t></is></c><c r="XFD1"><v>9</v></c></row>'
    assert _extract_xlsx(raw_xlsx([far])) == "[sheet: S1]\na" + "\t" * 16_383 + "9"
    assert _extract_xlsx(raw_xlsx([far * 100]), cap=10_000) == "attachment_too_large"
    # A reference past Excel's last column reads as the next column.
    past = '<row><c r="A1"><v>1</v></c><c r="ZZZZ1"><v>2</v></c></row>'
    assert _extract_xlsx(raw_xlsx([past])) == "[sheet: S1]\n1\t2"


# ── No network ─────────────────────────────────────────────────────────


def test_extraction_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("extraction tried to use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    items = [
        _item("a.pdf", make_pdf(["alpha"])),
        _item("b.docx", make_docx(["beta"])),
        _item("c.xlsx", make_xlsx({"S": [["gamma"]]})),
    ]
    texts = [d.text for d in decode_attachments(items, LIMITS)]
    assert texts == ["[page 1 of 1]\nalpha", "beta", "[sheet: S]\ngamma"]


# ── Through the daemon ─────────────────────────────────────────────────


async def test_a_pdf_on_a_user_message_reaches_the_loop_as_text(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)
    sess = daemon.session_registry.get(session_id)
    assert sess is not None
    captured: list[QueuedUserMessage] = []
    original = sess.add_user_message

    async def spy(content: str | QueuedUserMessage, **kwargs: bool) -> None:
        captured.append(QueuedUserMessage.plain(content) if isinstance(content, str) else content)
        await original(content, **kwargs)

    sess.add_user_message = spy  # type: ignore[method-assign]
    reply = await _send(daemon, session_id, [attach("r.pdf", make_pdf(["Total: 42"]))])
    assert reply is None
    assert "[page 1 of 1]\nTotal: 42" in captured[0].display
    assert captured[0].provider == captured[0].display
    await daemon._shutdown()


async def test_extract_attachment_answers_with_a_token_count(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)

    async def ask(name: str, data: bytes) -> dict[str, Any]:
        raw = json.dumps(
            {
                "type": "extract_attachment",
                "session_id": session_id,
                "request_id": "r1",
                "attachment": attach(name, data),
            }
        )
        assert isinstance(parse_client_message(raw).type, str)
        window = _Window()
        assert await daemon._handle_message(raw, window) is None
        return await window.next()

    ok = await ask("t.xlsx", make_xlsx({"Sheet A": [["abcd" * 10]], "B": [[1]]}))
    text = "[sheet: Sheet A]\n" + "abcd" * 10 + "\n[sheet: B]\n1"
    assert ok == {
        "type": "attachment_extracted",
        "seq": 1,
        "session_id": session_id,
        "request_id": "r1",
        "name": "t.xlsx",
        "kind": "xlsx",
        "units": 2,
        "text_bytes": len(text),
        "tokens": -(-len(text) // 4),
        "error_code": None,
        "message": "",
    }
    refused = await ask("scan.pdf", make_pdf([""]))
    assert refused["error_code"] == "attachment_no_text"
    assert refused["tokens"] == 0
    plain = await ask("a.txt", b"hello")
    assert plain["error_code"] == "attachment_not_document"
    await daemon._shutdown()


def test_extract_attachment_reaches_another_computers_session() -> None:
    assert "extract_attachment" in MACHINE_PEER_VERBS
    assert "extract_attachment" in FORWARDED_VERBS


class _Window:
    """A connection that keeps what the daemon sends it."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, frame: str) -> None:
        self.frames.put_nowait(frame)

    async def next(self) -> dict[str, Any]:
        return dict(json.loads(await asyncio.wait_for(self.frames.get(), timeout=10)))


def _gate(monkeypatch: pytest.MonkeyPatch, target: str) -> tuple[threading.Event, threading.Event]:
    """Hold *target* in its worker thread until released: (entered, release)."""
    module, attr = target.rsplit(".", 1)
    real = getattr(__import__(module, fromlist=[attr]), attr)
    entered, release = threading.Event(), threading.Event()

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, held)
    return entered, release


async def _until_set(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never reached")


async def test_a_chip_being_measured_does_not_hold_up_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)
    entered, release = _gate(monkeypatch, "tstd.attachment_handlers.measure_document")
    window = _Window()
    raw = json.dumps(
        {
            "type": "extract_attachment",
            "session_id": session_id,
            "request_id": "r1",
            "attachment": attach("a.pdf", make_pdf(["slow"])),
        }
    )
    # Returns at once although the document is still being read...
    assert await asyncio.wait_for(daemon._handle_message(raw, window), timeout=2) is None
    await _until_set(entered)
    # ...so the same connection's next frame is answered meanwhile.
    listed = await asyncio.wait_for(
        daemon._handle_message(json.dumps({"type": "list_sessions"}), window), timeout=2
    )
    assert listed is not None and json.loads(listed)["type"] == "session_list"
    assert window.frames.empty()
    release.set()
    answer = await window.next()
    assert (answer["type"], answer["request_id"], answer["units"]) == (
        "attachment_extracted",
        "r1",
        1,
    )
    await daemon._shutdown()


async def _send_held(
    daemon: Daemon, session_id: str, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> tuple[asyncio.Task[str | None], threading.Event]:
    """A user_message with a document, parked inside its extraction."""
    entered, release = _gate(monkeypatch, "tstd.daemon.decode_attachments")
    raw = json.dumps(
        {
            "type": "user_message",
            "session_id": session_id,
            "content": "read this",
            "attachments": [attach("r.pdf", make_pdf(["Total: 42"]))],
            **extra,
        }
    )
    task = asyncio.create_task(daemon._handle_message(raw, None))
    await _until_set(entered)
    return task, release


async def _queued(daemon: Daemon, session_id: str) -> list[QueuedUserMessage]:
    sess = daemon.session_registry.get(session_id)
    assert sess is not None
    captured: list[QueuedUserMessage] = []
    original = sess.add_user_message

    async def spy(content: str | QueuedUserMessage, **kwargs: bool) -> None:
        captured.append(QueuedUserMessage.plain(content) if isinstance(content, str) else content)
        await original(content, **kwargs)

    sess.add_user_message = spy  # type: ignore[method-assign]
    return captured


async def test_a_drain_begun_during_extraction_refuses_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)
    captured = await _queued(daemon, session_id)
    task, release = await _send_held(daemon, session_id, monkeypatch)
    daemon._drain.hold_for(60)
    release.set()
    reply = await asyncio.wait_for(task, timeout=10)
    assert reply is not None
    assert json.loads(reply)["code"] == "daemon_draining"
    assert captured == []
    daemon._drain.hold_for(0)
    await daemon._shutdown()


async def test_a_session_ended_during_extraction_refuses_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)
    captured = await _queued(daemon, session_id)
    task, release = await _send_held(daemon, session_id, monkeypatch)
    runner = daemon.session_registry.get_runner(session_id)
    assert runner is not None
    await runner.cancel()
    release.set()
    reply = await asyncio.wait_for(task, timeout=10)
    assert reply is not None
    assert json.loads(reply)["code"] == "session_not_running"
    assert captured == []
    await daemon._shutdown()


async def test_a_turn_begun_during_extraction_refuses_an_only_if_idle_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = await _open(daemon, workspace)
    sess = daemon.session_registry.get(session_id)
    assert sess is not None and not sess.turn_in_flight
    captured = await _queued(daemon, session_id)
    task, release = await _send_held(daemon, session_id, monkeypatch, only_if_idle=True)
    # Another sender gets in while the document is read (TD-5160's race).
    sess._open_turns += 1
    release.set()
    reply = await asyncio.wait_for(task, timeout=10)
    assert reply is not None
    assert json.loads(reply)["code"] == BUSY_CODE
    assert captured == []
    sess._open_turns -= 1
    await daemon._shutdown()
