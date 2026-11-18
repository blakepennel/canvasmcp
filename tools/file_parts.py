"""Split a course file into parts that fit a size budget without breaking pages or slides, and extract its text.

read_course_file only uses this when a file is too big to send whole (or when text is asked for), so a file that
fits is always sent as it is. Parts are worked out from the file's bytes alone, so the same file always splits the
same way and part numbers stay stable between calls.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from xml.etree import ElementTree

PDF, PPTX, DOCX, TEXT, MEDIA = "pdf", "pptx", "docx", "text", "media"
SPLITTABLE = (PDF, PPTX, TEXT)  # kinds that can be sent in parts as real files
EXTRACTABLE = (PDF, PPTX, DOCX, TEXT)  # kinds whose text can be read
UNIT = {PDF: "page", PPTX: "slide", DOCX: "paragraph", TEXT: "line", MEDIA: "caption"}
TEXT_EXTENSIONS = {
    "txt", "md", "csv", "tsv", "json", "xml", "html", "htm", "py", "java", "c", "h", "cpp", "hpp", "js", "ts",
    "go", "rs", "rb", "sh", "sql", "yaml", "yml", "tex", "r", "m", "ipynb", "log",
}
TEXT_TYPES = ("application/json", "application/xml", "application/javascript", "application/x-ipynb+json")


def file_kind(content_type: str, name: str) -> str | None:
    content_type = (content_type or "").split(";")[0].strip().lower()
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if content_type == "application/pdf" or extension == "pdf":
        return PDF
    if "presentationml.presentation" in content_type or extension == "pptx":
        return PPTX
    if "wordprocessingml.document" in content_type or extension == "docx":
        return DOCX
    if content_type.startswith(("video/", "audio/")):
        return MEDIA
    if content_type.startswith("text/") or content_type in TEXT_TYPES or extension in TEXT_EXTENSIONS:
        return TEXT
    return None


# -- packing -------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Part:
    first: int  # 1-based unit numbers, inclusive
    last: int
    size: int
    fits: bool  # False only for a single unit that is over the budget on its own


def pack(sizes: list[int], budget: int) -> list[tuple[int, int]]:
    """Consecutive units grouped greedily so each group's estimated size stays within budget (1-based, inclusive)."""
    ranges: list[tuple[int, int]] = []
    start, total = 1, 0
    for number, size in enumerate(sizes, start=1):
        if number > start and total + size > budget:
            ranges.append((start, number - 1))
            start, total = number, 0
        total += size
    if sizes:
        ranges.append((start, len(sizes)))
    return ranges


def plan(sizes: list[int], budget: int, build: Callable[[int, int], bytes]) -> list[Part]:
    """Pack by estimate, build each part and halve any that comes out over budget, then merge neighbours that fit.

    Estimates count shared images and fonts once per page, so they run high; merging gets back to as few parts as
    the real sizes allow.
    """
    parts: list[Part] = []
    pending = pack(sizes, budget)
    while pending:
        first, last = pending.pop(0)
        size = len(build(first, last))
        if size <= budget or first == last:
            parts.append(Part(first, last, size, size <= budget))
            continue
        middle = (first + last) // 2
        pending[:0] = [(first, middle), (middle + 1, last)]
    index = 0
    while index < len(parts) - 1:
        left, right = parts[index], parts[index + 1]
        if left.fits and right.fits and left.size + right.size <= budget * 2:
            size = len(build(left.first, right.last))
            if size <= budget:
                parts[index : index + 2] = [Part(left.first, right.last, size, True)]
                continue
        index += 1
    return parts


# -- PDF -----------------------------------------------------------------------------------------------
class PdfFile:
    def __init__(self, data: bytes) -> None:
        from pypdf import PdfReader

        self.reader = PdfReader(io.BytesIO(data))
        self.count = len(self.reader.pages)

    def build(self, first: int, last: int) -> bytes:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(first - 1, last):
            writer.add_page(self.reader.pages[index])
        writer.compress_identical_objects()
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    def sizes(self) -> list[int]:
        # Each page on its own carries every image and font it uses, so this over-counts shared ones: safe.
        return [len(self.build(number, number)) for number in range(1, self.count + 1)]

    def texts(self) -> list[str]:
        return [(page.extract_text() or "").strip() for page in self.reader.pages]


# -- PowerPoint ----------------------------------------------------------------------------------------
_SHARED_PARTS = ("slideLayout", "slideMaster", "notesMaster", "theme", "handoutMaster")


class PptxFile:
    def __init__(self, data: bytes) -> None:
        from pptx import Presentation

        self.data = data
        self.deck = Presentation(io.BytesIO(data))
        self.count = len(self.deck.slides)

    def build(self, first: int, last: int) -> bytes:
        from pptx import Presentation

        deck = Presentation(io.BytesIO(self.data))
        slide_ids = deck.slides._sldIdLst
        for index, slide_id in reversed(list(enumerate(list(slide_ids)))):
            if not first - 1 <= index <= last - 1:
                deck.part.drop_rel(slide_id.rId)  # unreferenced slides (and their media) aren't saved
                slide_ids.remove(slide_id)
        out = io.BytesIO()
        deck.save(out)
        return out.getvalue()

    def sizes(self) -> list[int]:
        sizes = []
        for slide in self.deck.slides:
            seen: set[str] = set()
            stack = [slide.part]
            total = 0
            while stack:
                part = stack.pop()
                if str(part.partname) in seen:
                    continue
                seen.add(str(part.partname))
                total += len(part.blob)
                for rel in part.rels.values():
                    if not rel.is_external and not any(name in rel.reltype for name in _SHARED_PARTS):
                        stack.append(rel.target_part)
            sizes.append(total)
        return sizes

    def texts(self) -> list[str]:
        out = []
        for slide in self.deck.slides:
            lines = []
            for shape in slide.shapes:
                lines += _shape_text(shape)
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip() if slide.notes_slide.notes_text_frame else ""
                if notes:
                    lines.append(f"Speaker notes: {notes}")
            out.append("\n".join(line for line in lines if line))
        return out


def _shape_text(shape) -> list[str]:
    if getattr(shape, "shape_type", None) == 6:  # group
        return [line for child in shape.shapes for line in _shape_text(child)]
    if getattr(shape, "has_table", False) and shape.has_table:
        return [" | ".join(cell.text.strip() for cell in row.cells) for row in shape.table.rows]
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        return [p.strip() for p in shape.text_frame.text.splitlines() if p.strip()]
    return []


# -- Word ----------------------------------------------------------------------------------------------
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_texts(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(f"{_W}p"):
        text = "".join(
            node.text or ("\t" if node.tag == f"{_W}tab" else "")
            for node in paragraph.iter()
            if node.tag in (f"{_W}t", f"{_W}tab")
        ).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


# -- plain text ----------------------------------------------------------------------------------------
class TextFile:
    def __init__(self, data: bytes) -> None:
        self.lines = data.splitlines(keepends=True)
        self.count = len(self.lines)

    def build(self, first: int, last: int) -> bytes:
        return b"".join(self.lines[first - 1 : last])

    def sizes(self) -> list[int]:
        return [len(line) for line in self.lines]

    def texts(self) -> list[str]:
        return [line.decode("utf-8", errors="replace").rstrip("\r\n") for line in self.lines]


def open_file(kind: str, data: bytes) -> PdfFile | PptxFile | TextFile:
    return {PDF: PdfFile, PPTX: PptxFile, TEXT: TextFile}[kind](data)


def unit_texts(kind: str, data: bytes) -> list[str]:
    if kind == DOCX:
        return docx_texts(data)
    return open_file(kind, data).texts()


# -- captions ------------------------------------------------------------------------------------------
_CUE_TIME = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d{1,3}\s*-->")


def caption_lines(track: str) -> list[str]:
    """WebVTT or SRT captions as "[h:mm:ss] text" lines."""
    lines: list[str] = []
    stamp: str | None = None
    text: list[str] = []

    def flush() -> None:
        if stamp is not None and text:
            lines.append(f"[{stamp}] {' '.join(text)}")

    for raw in track.splitlines():
        line = raw.strip()
        match = _CUE_TIME.match(line)
        if match:
            flush()
            hours, minutes, seconds = match.group(1) or "0", match.group(2), match.group(3)
            stamp, text = f"{int(hours)}:{int(minutes):02d}:{seconds}", []
        elif not line:
            flush()
            stamp, text = None, []
        elif stamp is not None and not line.isdigit():
            text.append(re.sub(r"<[^>]+>", "", line))
    flush()
    return lines


def text_parts(texts: list[str], budget: int) -> list[Part]:
    sizes = [len(text) + 40 for text in texts]  # 40 for the "--- Page N ---" label
    return [Part(first, last, sum(sizes[first - 1 : last]), True) for first, last in pack(sizes, budget)]


def join_texts(kind: str, texts: list[str], first: int, last: int) -> str:
    if kind in (TEXT, MEDIA, DOCX):
        return "\n".join(texts[first - 1 : last])
    label = UNIT[kind].capitalize()
    return "\n\n".join(f"--- {label} {n} ---\n{texts[n - 1] or '(no text)'}" for n in range(first, last + 1))
