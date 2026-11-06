"""Text extraction: real parsers, in a bounded subprocess, never raising into the store."""

from __future__ import annotations

from pathlib import Path

import docx
from coppermind_store.extraction import EXTRACTOR_VERSION, extract, extractable, media_type
from pptx import Presentation


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a text object and a correct xref table."""
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def deck(path: Path, title: str, body: str) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = title
    slide.placeholders[1].text = body
    presentation.save(str(path))


def document(path: Path, *paragraphs: str) -> None:
    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(path))


def test_slide_text_is_extracted_from_a_pptx(tmp_path):
    path = tmp_path / "deck.pptx"
    deck(path, "VCDX design package", "Storage: vSAN stretched cluster")
    text, status = extract(path, media_type("deck.pptx", None, path.read_bytes()))
    assert status == "extracted"
    assert "Slide 1" in text
    assert "VCDX design package" in text
    assert "vSAN stretched cluster" in text


def test_paragraphs_are_extracted_from_a_docx(tmp_path):
    path = tmp_path / "howto.docx"
    document(path, "ADFS claims rules", "for Farm Bureau")
    text, status = extract(path, media_type("howto.docx", None, path.read_bytes()))
    assert status == "extracted"
    assert text == "ADFS claims rules\nfor Farm Bureau"


def test_page_text_is_extracted_from_a_pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(minimal_pdf("Upgrade deck for vSphere"))
    text, status = extract(path, "application/pdf")
    assert (text, status) == ("Upgrade deck for vSphere", "extracted")


def test_text_types_decode_and_images_are_unsupported(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("plain text body", encoding="utf-8")
    assert extract(notes, "text/plain") == ("plain text body", "extracted")
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    assert extract(image, "image/png") == ("", "unsupported")
    assert not extractable("image/png")
    assert extractable("application/pdf")


def test_a_corrupt_document_fails_closed_rather_than_raising(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\ngarbage")
    text, status = extract(path, "application/pdf")
    assert (text, status) == ("", "failed")
    empty = tmp_path / "empty.pptx"
    empty.write_bytes(b"PK\x03\x04 not really a deck")
    assert extract(empty, media_type("empty.pptx", None, empty.read_bytes()))[1] == "failed"


def test_media_type_prefers_the_declared_type_then_the_bytes_then_the_extension():
    pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert media_type("x.bin", "application/pdf; charset=binary", b"junk") == "application/pdf"
    assert media_type("x.bin", None, b"%PDF-1.7 ...") == "application/pdf"
    assert media_type("deck.pptx", "application/octet-stream", b"PK\x03\x04...") == pptx
    assert media_type("photo.JPG", None, b"\xff\xd8") == "image/jpeg"
    assert media_type("mystery", None, b"\x00\x01") == "application/octet-stream"


def test_the_extractor_version_is_a_plain_token():
    assert EXTRACTOR_VERSION.strip() == EXTRACTOR_VERSION and ":" not in EXTRACTOR_VERSION
