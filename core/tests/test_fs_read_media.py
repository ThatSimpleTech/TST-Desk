"""fs_read opens images, PDFs and notebooks (TD-5130).

Before TD-5130 every one of these came back as "binary file" or a raw
JSON dump. Each test drives the real handler, the dispatcher, or the
loop with the mock provider, and checks what the model would receive.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_context_fit import _tier
from tests.test_loop import mock_factory, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.config import ModelConfig, Preset
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import fs_read, handlers
from tstd.tools.read_pdf import PDF_MAX_PAGES, encode_png
from tstd.tools.results import HandlerOutput

# A 2x1 RGB PNG, built here so the bytes are known.
PNG = encode_png(2, 1, 6, bytes([255, 0, 0, 0, 0, 255]))
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32 + b"\xff\xd9"


def vision(max_images: int = 1) -> SimpleNamespace:
    return SimpleNamespace(tool_vision=True, tool_max_images=max_images)


def text_only() -> SimpleNamespace:
    return SimpleNamespace(tool_vision=False, tool_max_images=1)


def make_pdf(page_texts: list[str]) -> bytes:
    """A small, valid PDF with one line of Helvetica text per page."""
    n = len(page_texts)
    font_id = 3 + 2 * n
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
            + f"] /Count {n} >>"
        ).encode(),
    ]
    for i, text in enumerate(page_texts):
        content_id = 4 + 2 * i
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


NOTEBOOK = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {"language_info": {"name": "python"}},
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n", "Some prose."]},
        {
            "cell_type": "code",
            "execution_count": 1,
            "metadata": {},
            "source": ["x = 6 * 7\n", "print(x)\n", "x"],
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": ["42\n"]},
                {
                    "output_type": "execute_result",
                    "execution_count": 1,
                    "metadata": {},
                    "data": {"text/plain": ["42"], "text/html": ["<b>42</b>"]},
                },
                {
                    "output_type": "display_data",
                    "metadata": {},
                    "data": {"image/png": "iVBORw0KGgo="},
                },
            ],
        },
        {
            "cell_type": "code",
            "execution_count": 2,
            "metadata": {},
            "source": "1 / 0",
            "outputs": [
                {
                    "output_type": "error",
                    "ename": "ZeroDivisionError",
                    "evalue": "division by zero",
                    "traceback": ["\x1b[0;31mZeroDivisionError\x1b[0m: division by zero"],
                }
            ],
        },
    ],
}


# ── Images ──────────────────────────────────────────────────────────────


class TestImages:
    async def test_vision_tier_gets_the_image(self, tmp_path: Path) -> None:
        f = tmp_path / "shot.png"
        f.write_bytes(PNG)
        out = await fs_read(vision(), str(f))
        assert isinstance(out, HandlerOutput)
        assert len(out.images) == 1
        assert out.images[0].mime == "image/png"
        assert out.images[0].data == PNG
        assert "image/png" in out.text
        # The pixels never go into the text.
        assert base64.b64encode(PNG).decode() not in out.text

    async def test_type_comes_from_bytes_not_name(self, tmp_path: Path) -> None:
        f = tmp_path / "photo.png"
        f.write_bytes(JPEG)
        out = await fs_read(vision(), str(f))
        assert isinstance(out, HandlerOutput)
        assert out.images[0].mime == "image/jpeg"

    async def test_text_only_tier_is_told_it_cannot(self, tmp_path: Path) -> None:
        f = tmp_path / "shot.png"
        f.write_bytes(PNG)
        out = await fs_read(text_only(), str(f))
        assert isinstance(out, str)
        assert "does not accept images" in out
        assert "binary" not in out

    async def test_no_session_is_text_only(self, tmp_path: Path) -> None:
        f = tmp_path / "shot.png"
        f.write_bytes(PNG)
        out = await fs_read(None, str(f))
        assert isinstance(out, str) and "does not accept images" in out

    async def test_zero_image_cap_counts_as_no_vision(self, tmp_path: Path) -> None:
        f = tmp_path / "shot.png"
        f.write_bytes(PNG)
        out = await fs_read(vision(max_images=0), str(f))
        assert isinstance(out, str) and "does not accept images" in out

    async def test_oversized_image_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(handlers, "FS_READ_MAX_IMAGE_BYTES", 10)
        f = tmp_path / "big.png"
        f.write_bytes(PNG)
        out = await fs_read(vision(), str(f))
        assert isinstance(out, str) and "limit for an image read" in out

    async def test_other_binary_still_refused(self, tmp_path: Path) -> None:
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\x00\x01\x02")
        out = await fs_read(vision(), str(f))
        assert isinstance(out, str) and "binary file" in out


# ── PDFs ────────────────────────────────────────────────────────────────


class TestPdf:
    async def test_text_of_every_page_with_markers(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["Alpha page", "Bravo page", "Charlie page"]))
        out = await fs_read(text_only(), str(f))
        assert isinstance(out, str)
        assert "3 pages" in out
        assert "── page 1 ──\nAlpha page" in out
        assert "── page 3 ──\nCharlie page" in out
        assert "continue with" not in out

    async def test_page_range(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["Alpha", "Bravo", "Charlie", "Delta"]))
        out = await fs_read(text_only(), str(f), pages="2-3")
        assert isinstance(out, str)
        assert "Bravo" in out and "Charlie" in out
        assert "Alpha" not in out and "Delta" not in out
        assert 'continue with pages="4-4"' in out
        one = await fs_read(text_only(), str(f), pages="4")
        assert isinstance(one, str) and "Delta" in one and "Charlie" not in one

    async def test_default_is_ten_pages_and_long_range_is_cut(self, tmp_path: Path) -> None:
        f = tmp_path / "long.pdf"
        f.write_bytes(make_pdf([f"Page{n}" for n in range(1, 26)]))
        first = await fs_read(text_only(), str(f))
        assert isinstance(first, str)
        assert "Page10" in first and "Page11" not in first
        assert 'continue with pages="11-25"' in first
        wide = await fs_read(text_only(), str(f), pages="1-25")
        assert isinstance(wide, str)
        assert f"Page{PDF_MAX_PAGES}" in wide and f"Page{PDF_MAX_PAGES + 1}" not in wide

    async def test_dense_pdf_fits_the_cap_and_names_the_next_page(self, tmp_path: Path) -> None:
        # Each page's text is ~400 characters; a 1,500-character cap holds
        # three. Before the fix the dispatcher cut the text mid-page and its
        # offset/limit marker replaced the note naming the next range.
        (tmp_path / "dense.pdf").write_bytes(
            make_pdf([f"P{n:02d} " + "word " * 80 for n in range(1, 31)])
        )
        dispatcher = make_dispatcher(tmp_path)
        dispatcher.max_result_chars = 1500
        result = await dispatcher.dispatch(
            "t1", "fs_read", {"path": "dense.pdf"}, session=text_only()
        )
        out = result.output
        assert len(out) <= 1500
        assert "offset/limit" not in out and "truncated" not in out
        assert "pages 1-2 below" in out and "P02" in out and "P03" not in out
        assert 'continue with pages="3-22"' in out
        assert "did not fit in one result" in out

    async def test_dense_pdf_images_only_of_pages_shown(self, tmp_path: Path) -> None:
        f = tmp_path / "dense.pdf"
        f.write_bytes(make_pdf([f"P{n:02d} " + "word " * 80 for n in range(1, 6)]))
        out = handlers._read_pdf_file(f, "", True, vision=True, vision_slots=5, max_chars=1500)
        assert isinstance(out, HandlerOutput)
        assert len(out.images) == 2
        assert "images of page 1, 2 follow" in out.text
        assert 'continue with pages="3-5"' in out.text

    async def test_one_page_larger_than_the_cap_is_cut_and_said(self, tmp_path: Path) -> None:
        f = tmp_path / "huge.pdf"
        f.write_bytes(make_pdf(["word " * 300, "next"]))
        out = handlers._read_pdf_file(f, "", False, vision=False, vision_slots=0, max_chars=1000)
        assert isinstance(out, str) and len(out) <= 1000
        assert "page 1's text was cut" in out
        assert 'continue with pages="2-2"' in out

    @pytest.mark.parametrize("pages", ["0", "5-2", "abc", "9"])
    async def test_bad_range_is_an_error(self, tmp_path: Path, pages: str) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["A", "B", "C"]))
        out = await fs_read(text_only(), str(f), pages=pages)
        assert isinstance(out, str) and out.startswith("Error:")

    async def test_damaged_pdf_is_an_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.pdf"
        f.write_bytes(b"%PDF-1.4\n\x00garbage")
        out = await fs_read(text_only(), str(f))
        assert isinstance(out, str) and out.startswith("Error: could not read PDF")

    async def test_a_page_pdfium_cannot_read_is_an_error_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pypdfium2 as pdfium

        from tstd.tools import read_pdf as read_pdf_module

        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["one", "two"]))

        def broken(page: object) -> str:
            raise pdfium.PdfiumError("Failed to load page.")

        monkeypatch.setattr(read_pdf_module, "_page_text", broken)
        out = await fs_read(text_only(), str(f))
        assert isinstance(out, str) and out.startswith("Error: could not read PDF")

    async def test_page_images_on_vision_tier(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["Alpha", "Bravo", "Charlie"]))
        out = await fs_read(vision(max_images=2), str(f), page_images=True)
        assert isinstance(out, HandlerOutput)
        assert "Alpha" in out.text  # text is still there
        assert len(out.images) == 2  # bounded by the prompt's image cap
        assert all(i.mime == "image/png" and i.data.startswith(b"\x89PNG") for i in out.images)
        assert "images of page 1, 2 follow" in out.text
        assert "1 more page(s)" in out.text

    async def test_no_page_images_unless_asked(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["Alpha"]))
        out = await fs_read(vision(), str(f))
        assert isinstance(out, str)

    async def test_page_images_on_text_tier_say_why_not(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(make_pdf(["Alpha"]))
        out = await fs_read(text_only(), str(f), page_images=True)
        assert isinstance(out, str)
        assert "Alpha" in out and "does not accept images" in out


# ── Notebooks ───────────────────────────────────────────────────────────


class TestNotebook:
    async def test_cells_and_text_outputs(self, tmp_path: Path) -> None:
        f = tmp_path / "nb.ipynb"
        f.write_text(json.dumps(NOTEBOOK), encoding="utf-8")
        out = await fs_read(None, str(f))
        assert isinstance(out, str)
        assert out.startswith("Notebook: 3 cells, python.")
        assert "── cell 1 · markdown ──" in out
        assert "# Title" in out
        assert "── cell 2 · code · In [1] ──" in out
        assert "print(x)" in out
        assert "── output · stdout ──" in out
        assert "── output · result · text/plain ──" in out
        assert "<b>42</b>" not in out  # HTML is skipped for its plain form
        assert "[image/png output, not shown]" in out
        assert "ZeroDivisionError: division by zero" in out
        assert "\x1b[" not in out  # colour codes stripped
        assert '"nbformat"' not in out  # not the raw JSON

    async def test_line_window_over_the_view(self, tmp_path: Path) -> None:
        f = tmp_path / "nb.ipynb"
        f.write_text(json.dumps(NOTEBOOK), encoding="utf-8")
        out = await fs_read(None, str(f), offset=2, limit=1)
        assert isinstance(out, str)
        body = out.split("\n")
        assert body[1] == "2: # Title"
        assert "continue with offset=3" in out

    async def test_not_a_notebook_reads_as_text(self, tmp_path: Path) -> None:
        f = tmp_path / "odd.ipynb"
        f.write_text('{"hello": 1}\n', encoding="utf-8")
        out = await fs_read(None, str(f))
        assert out == '1: {"hello": 1}'


# ── Through dispatch and the loop ───────────────────────────────────────


class TestDispatch:
    async def test_result_carries_image_not_base64_text(self, tmp_path: Path) -> None:
        (tmp_path / "shot.png").write_bytes(PNG)
        dispatcher = make_dispatcher(tmp_path)
        session = Session(str(tmp_path))
        session.tool_vision = True
        result = await dispatcher.dispatch("t1", "fs_read", {"path": "shot.png"}, session=session)
        assert result.status == "success"
        assert [i.mime for i in result.images] == ["image/png"]
        assert base64.b64encode(PNG).decode() not in result.output


def _config(vision_on: bool, worker_vision: bool | None = None) -> ModelConfig:
    tier = _tier().model_copy(update={"vision": vision_on})
    worker = tier.model_copy(
        update={"vision": vision_on if worker_vision is None else worker_vision}
    )
    return ModelConfig(
        presets={"test": Preset(brain=tier, worker=worker, validator=tier)},
        active_preset="test",
    )


async def _run_read(
    workspace: Path,
    vision_on: bool,
    *,
    worker_vision: bool | None = None,
    lead_turns: int = 1_000,
) -> object:
    """One turn: the model reads shot.png, then answers. The second
    request's tool message is what the model was given."""
    (workspace / "shot.png").write_bytes(PNG)
    mock = MockProvider(
        sequences={
            "gateway-model": [
                Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments=json.dumps({"path": "shot.png"}),
                ),
                Script(kind="stream", content="A red and a blue pixel."),
            ]
        }
    )
    session = Session(str(workspace))
    dispatcher = make_dispatcher(workspace)
    config = _config(vision_on, worker_vision)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(lead_turns=lead_turns),
            mock_factory(mock),
            config,
            tool_registry=dispatcher.registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    try:
        await session.add_user_message("Look at shot.png.")
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is False
    finally:
        await runner.cancel()
    assert len(mock.calls) == 2
    tool_messages = [m for m in mock.calls[1].messages if m.role == "tool"]
    assert len(tool_messages) == 1
    return tool_messages[0].content


class TestLoop:
    async def test_vision_tier_model_sees_the_image(self, tmp_path: Path) -> None:
        content = await _run_read(tmp_path, vision_on=True)
        assert isinstance(content, list)
        kinds = [part["type"] for part in content]
        assert kinds == ["text", "image_url"]
        url = content[1]["image_url"]["url"]
        assert url == "data:image/png;base64," + base64.b64encode(PNG).decode()

    async def test_text_tier_model_is_told_it_cannot(self, tmp_path: Path) -> None:
        content = await _run_read(tmp_path, vision_on=False)
        assert isinstance(content, str)
        assert "does not accept images" in content

    async def test_the_tier_the_result_goes_to_decides_not_the_one_that_asked(
        self, tmp_path: Path
    ) -> None:
        """The lead turn ends mid-turn: a vision brain asks, a text worker reads."""
        content = await _run_read(tmp_path, vision_on=True, worker_vision=False, lead_turns=1)
        assert isinstance(content, str)
        assert "does not accept images" in content


def test_screenshot_then_file_images_follow_the_text() -> None:
    from tstd.loop import _tool_message_content
    from tstd.tools.results import ToolImage, ToolResult

    result = ToolResult(
        tool_call_id="t",
        name="fs_read",
        status="success",
        output="two pictures",
        image_png=PNG,
        images=(ToolImage("image/jpeg", JPEG),),
    )
    content = _tool_message_content(result)
    assert [p["type"] for p in content] == ["text", "image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
