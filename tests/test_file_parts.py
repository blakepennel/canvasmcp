from __future__ import annotations

import base64
import io
import os
import zipfile
from unittest import mock

import pytest

from tools import file_parts


# -- building test files -------------------------------------------------------------------------------
def make_pdf(page_kb: list[int]) -> bytes:
    """A PDF whose page N carries page_kb[N] KB of incompressible content and the text 'Page N'."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
         NameObject("/BaseFont"): NameObject("/Helvetica")}
    )
    for number, kb in enumerate(page_kb, start=1):
        page = writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject()
        filler = os.urandom(kb * 512).hex().encode()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Page %d) Tj ET\n%% " % number + filler)
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_pptx(slide_kb: list[int]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for number, kb in enumerate(slide_kb, start=1):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = f"Slide {number}"
        if kb:
            image = _png(kb)
            slide.shapes.add_picture(io.BytesIO(image), Inches(1), Inches(2))
        slide.notes_slide.notes_text_frame.text = f"note {number}"
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def _png(kb: int) -> bytes:
    """A valid PNG of about kb KB that doesn't compress (random pixels)."""
    import struct
    import zlib

    side = max(8, int((kb * 1024 / 3) ** 0.5))
    raw = b"".join(b"\x00" + os.urandom(side * 3) for _ in range(side))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b"")


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


# -- file_parts ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("content_type", "name", "kind"),
    [
        ("application/pdf", "a.pdf", "pdf"),
        ("application/octet-stream", "Slides.PPTX", "pptx"),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "x", "pptx"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "x", "docx"),
        ("video/mp4", "talk.mp4", "media"),
        ("audio/mpeg", "a.mp3", "media"),
        ("text/plain; charset=utf-8", "notes", "text"),
        ("application/octet-stream", "main.py", "text"),
        ("application/zip", "code.zip", None),
        ("image/png", "a.png", None),
    ],
)
def test_file_kind(content_type, name, kind):
    assert file_parts.file_kind(content_type, name) == kind


def test_pack_keeps_order_and_budget():
    assert file_parts.pack([3, 3, 3, 3], 6) == [(1, 2), (3, 4)]
    assert file_parts.pack([10, 1, 1], 6) == [(1, 1), (2, 3)]  # an oversized unit gets its own part
    assert file_parts.pack([], 6) == []


def test_plan_halves_parts_that_come_out_too_big():
    built = []

    def build(first, last):
        built.append((first, last))
        return b"x" * (5 * (last - first + 1))  # real size is bigger than the estimate of 1 per unit

    parts = file_parts.plan([1] * 4, 10, build)
    assert [(p.first, p.last) for p in parts] == [(1, 2), (3, 4)]
    assert all(p.fits for p in parts)


def test_plan_merges_neighbours_whose_real_size_fits():
    # Estimates of 6 per unit make three parts; built together they're small, so they merge into one.
    parts = file_parts.plan([6, 6, 6], 10, lambda first, last: b"x" * (last - first + 1))
    assert [(p.first, p.last, p.size) for p in parts] == [(1, 3, 3)]


def test_plan_marks_a_single_unit_over_budget():
    parts = file_parts.plan([50, 1], 10, lambda first, last: b"x" * (50 if first == 1 else 1))
    assert [(p.first, p.last, p.fits) for p in parts] == [(1, 1, False), (2, 2, True)]


def test_pdf_parts_are_whole_pages_within_budget():
    from pypdf import PdfReader

    document = file_parts.open_file("pdf", make_pdf([20, 20, 20, 20, 20]))
    parts = file_parts.plan(document.sizes(), 50 * 1024, document.build)
    assert len(parts) >= 2
    assert parts[0].first == 1 and parts[-1].last == 5
    assert all(b.first == a.last + 1 for a, b in zip(parts, parts[1:]))
    for p in parts:
        piece = document.build(p.first, p.last)
        assert len(piece) <= 50 * 1024
        assert len(PdfReader(io.BytesIO(piece)).pages) == p.last - p.first + 1


def test_pptx_parts_are_whole_slides_and_drop_other_slides_media():
    from pptx import Presentation

    document = file_parts.open_file("pptx", make_pptx([30, 30, 30, 0]))
    parts = file_parts.plan(document.sizes(), 80 * 1024, document.build)
    assert len(parts) >= 2
    for p in parts:
        piece = document.build(p.first, p.last)
        assert len(piece) <= 80 * 1024
        titles = [s.shapes.title.text for s in Presentation(io.BytesIO(piece)).slides]
        assert titles == [f"Slide {n}" for n in range(p.first, p.last + 1)]


def test_texts_per_type():
    assert "Slide 2" in file_parts.unit_texts("pptx", make_pptx([0, 0]))[1]
    assert "Speaker notes: note 1" in file_parts.unit_texts("pptx", make_pptx([0]))[0]
    assert file_parts.unit_texts("docx", make_docx(["One", "Two"])) == ["One", "Two"]
    assert file_parts.unit_texts("text", b"a\nb\n") == ["a", "b"]
    assert "Page 2" in file_parts.join_texts("pdf", ["x", "y"], 1, 2)


@pytest.mark.parametrize(
    "track",
    [
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello <b>there</b>\n\n01:02:03.500 --> 01:02:05.000\nLater\nline\n",
        "1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n2\n01:02:03,500 --> 01:02:05,000\nLater\nline\n",
    ],
)
def test_caption_lines(track):
    assert file_parts.caption_lines(track) == ["[0:00:01] Hello there", "[1:02:03] Later line"]


# -- read_course_file ----------------------------------------------------------------------------------
@pytest.fixture
def small_mb(monkeypatch):
    """Make 1 "MB" mean 1 KB so splitting can be tested with small files."""
    import tools.files

    monkeypatch.setattr(tools.files, "MB", 1024)
    monkeypatch.delenv("CANVAS_MCP_MAX_INLINE_MB", raising=False)
    tools.files._recent_files.clear()
    tools.files._recent_plans.clear()
    yield
    tools.files._recent_files.clear()
    tools.files._recent_plans.clear()


def _serve(mock_client, data, content_type="application/pdf", name="slides.pdf", **info):
    meta = {"display_name": name, "content-type": content_type, "size": len(data), "updated_at": "t", **info}

    def read(*, course_id, file_id, max_bytes):
        return meta, (data if len(data) <= max_bytes else None)

    mock_client.read_file_bytes.side_effect = read
    return meta


def _text(result):
    return "\n".join(c.text for c in result.content if c.type == "text")


def _blob(result):
    return next(base64.b64decode(c.resource.blob) for c in result.content if c.type == "resource")


def test_a_file_that_fits_is_sent_whole_without_splitting(mock_client, small_mb):
    from tools.files import read_course_file

    data = make_pdf([5, 5])
    _serve(mock_client, data)
    result = read_course_file("1", "2", max_size_mb=100)
    assert _blob(result) == data
    assert "part" not in _text(result)
    assert mock_client.read_file_bytes.call_count == 1


def test_a_big_pdf_comes_in_parts_and_part_1_lists_them(mock_client, small_mb):
    from pypdf import PdfReader

    from tools.files import read_course_file

    data = make_pdf([20, 20, 20, 20, 20])
    _serve(mock_client, data)
    first = read_course_file("1", "2", max_size_mb=50)
    text = _text(first)
    assert "comes in" in text and "keep whole pages together" in text and "Read the next with part=2" in text
    assert len(PdfReader(io.BytesIO(_blob(first))).pages) >= 1
    assert len(_blob(first)) <= 50 * 1024
    resource = next(c for c in first.content if c.type == "resource")
    assert str(resource.resource.uri).endswith("slides%20%28part%201%29.pdf")

    second = read_course_file("1", "2", max_size_mb=50, part=2)
    assert "This is part 2" in _text(second)
    # The whole file was downloaded once (plus one size check per call), then reused.
    big = [c for c in mock_client.read_file_bytes.call_args_list if c.kwargs["max_bytes"] > 50 * 1024]
    assert len(big) == 1


def test_every_page_is_in_exactly_one_part(mock_client, small_mb):
    from pypdf import PdfReader

    from tools.files import read_course_file

    _serve(mock_client, make_pdf([15] * 7))
    pages = []
    part = 1
    while True:
        try:
            result = read_course_file("1", "2", max_size_mb=40, part=part)
        except ValueError as exc:
            assert "doesn't exist" in str(exc)
            break
        reader = PdfReader(io.BytesIO(_blob(result)))
        pages += [p.extract_text() for p in reader.pages]
        part += 1
    assert pages == [f"Page {n}" for n in range(1, 8)]


def test_a_part_past_the_end_is_an_error(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, make_pdf([5]))
    with pytest.raises(ValueError, match="has 1 part; part 2 doesn't exist"):
        read_course_file("1", "2", max_size_mb=100, part=2)
    with pytest.raises(ValueError, match="part starts at 1"):
        read_course_file("1", "2", part=0)


def test_a_page_too_big_on_its_own_comes_as_text(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, make_pdf([80, 5]))
    result = read_course_file("1", "2", max_size_mb=40)
    text = _text(result)
    assert "Page 1 alone is" in text and "only its text is shown" in text and "Page 1" in text
    assert "part 1: page 1 (text only: too big to send), part 2: page 2" in text
    assert not any(c.type == "resource" for c in result.content)


def test_text_only_is_light_and_says_what_is_lost(mock_client, small_mb):
    from tools.files import read_course_file

    data = make_pdf([5, 5])
    _serve(mock_client, data)
    result = read_course_file("1", "2", text_only=True)
    text = _text(result)
    assert "--- Page 1 ---\nPage 1" in text and "--- Page 2 ---\nPage 2" in text
    assert "images, diagrams, equations, and layout are missing" in text
    assert not any(c.type == "resource" for c in result.content)


def test_text_only_works_for_word_files_too_big_to_send(mock_client, small_mb):
    from tools.files import read_course_file

    data = make_docx(["Intro", "Body"]) + b"\0" * 0
    _serve(mock_client, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx")
    result = read_course_file("1", "2", max_size_mb=1, text_only=True)
    assert "Intro\nBody" in _text(result)


def test_long_text_comes_in_parts(mock_client, small_mb, monkeypatch):
    import tools.files
    from tools.files import read_course_file

    monkeypatch.setattr(tools.files, "TEXT_PART_CHARS", 200)
    _serve(mock_client, b"".join(b"line %03d %s\n" % (n, b"x" * 40) for n in range(20)), "text/plain", "a.txt")
    first = _text(read_course_file("1", "2", text_only=True))
    assert "part 1 of" in first and "(next: part=2)" in first and "line 000" in first and "line 019" not in first


def test_an_unsplittable_file_that_is_too_big_says_so(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, b"z" * 5000, "application/zip", "code.zip")
    with pytest.raises(ValueError, match="can't be sent in parts.*download_course_file"):
        read_course_file("1", "2", max_size_mb=1)


def test_a_small_video_comes_with_its_captions(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, b"v" * 100, "video/mp4", "talk.mp4", media_entry_id="m-1")
    mock_client.list_media_tracks.return_value = [
        {"locale": "es", "content": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHola\n"},
        {"locale": "en", "content": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"},
    ]
    result = read_course_file("1", "2")
    assert _blob(result) == b"v" * 100
    assert "[0:00:01] Hello" in _text(result) and "Hola" not in _text(result)
    mock_client.list_media_tracks.assert_called_once_with(file_id="2")


def test_a_big_video_sends_its_captions_instead(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, b"v" * 5000, "video/mp4", "talk.mp4", media_entry_id="m-1")
    mock_client.list_media_tracks.return_value = [{"locale": "en", "content": "1\n00:00:01,000 --> 00:00:02,000\nHi\n"}]
    result = read_course_file("1", "2", max_size_mb=1)
    assert "too big to send, so here are Canvas's captions" in _text(result) and "[0:00:01] Hi" in _text(result)
    assert all(c.kwargs["max_bytes"] <= 1024 for c in mock_client.read_file_bytes.call_args_list)  # never downloaded


def test_a_big_video_without_captions_is_not_transcribed(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, b"v" * 5000, "video/mp4", "talk.mp4", media_entry_id="m-1")
    mock_client.list_media_tracks.return_value = []
    with pytest.raises(ValueError, match="Canvas has no captions for it. Use download_course_file"):
        read_course_file("1", "2", max_size_mb=1)


def test_a_file_without_a_media_entry_never_asks_for_captions(mock_client, small_mb):
    from tools.files import read_course_file

    _serve(mock_client, b"v" * 100, "video/mp4", "talk.mp4")
    read_course_file("1", "2")
    mock_client.list_media_tracks.assert_not_called()


def test_parts_respect_the_deployment_ceiling(mock_client, small_mb, monkeypatch):
    from tools.files import read_course_file

    monkeypatch.setenv("CANVAS_MCP_MAX_INLINE_MB", "30")
    _serve(mock_client, make_pdf([20, 20, 20]))
    result = read_course_file("1", "2", max_size_mb=50)
    assert "over the 30 MB limit" in _text(result)
    assert len(_blob(result)) <= 30 * 1024


# -- client --------------------------------------------------------------------------------------------
def _client():
    from client import CanvasClient

    requester = mock.MagicMock()
    canvas = mock.MagicMock()
    setattr(canvas, "_Canvas__requester", requester)
    return CanvasClient(base_url="https://canvas.example.test"), requester, mock.patch("client.base.Canvas", return_value=canvas)


def test_media_tracks_are_read_with_their_text():
    client, requester, patch = _client()
    requester.request.return_value.json.return_value = [{"locale": "en", "content": "WEBVTT"}]
    with patch:
        assert client.list_media_tracks(file_id="9") == [{"locale": "en", "content": "WEBVTT"}]
    call = requester.request.call_args
    assert call.args == ("GET", "media_attachments/9/media_tracks")
    assert call.kwargs["_kwargs"] == [("include[]", "content")]


def test_missing_media_tracks_are_an_empty_list():
    from canvasapi.exceptions import ResourceDoesNotExist

    client, requester, patch = _client()
    requester.request.side_effect = ResourceDoesNotExist("nope")
    with patch:
        assert client.list_media_tracks(file_id="9") == []
