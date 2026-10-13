"""Frozen main-pipeline golden outputs over generated non-glyph documents.

Golden source: pre-glyph detector and converter, captured for R4 from the
review's baseline checkout. No reference or repository history is read by tests.
"""

from dataclasses import asdict
from pathlib import Path

import fitz
import pytest
from click.testing import CliRunner

from legacylipi.cli import main
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.models import (
    DetectionMethod,
    EncodingDetectionResult,
    FontInfo,
    PDFDocument,
    PDFPage,
    TextBlock,
)
from legacylipi.core.pdf_parser import parse_pdf
from legacylipi.core.unicode_converter import UnicodeConverter
from tests.glyph_helpers import make_pdf

CASES = [
    "latin",
    "unicode",
    "kruti",
    "shree",
    "unused",
    "unused-reverse",
    "mixed",
    "mixed-reverse",
    "overlay",
    "scanned",
]


def corpus_pdf(path, case):
    with fitz.open() as doc:
        page = doc.new_page()
        if case in ("overlay", "scanned"):
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
            pix.clear_with(255)
            page.insert_image(fitz.Rect(10, 10, 300, 500), pixmap=pix)
        if case in ("unused", "unused-reverse"):
            fonts = ["helv", "cour"] if case == "unused-reverse" else ["cour", "helv"]
            for font in fonts:
                ref = page.insert_font(fontname=font)
                if font == "cour":
                    doc.xref_set_key(ref, "BaseFont", "/ShreeDev0708")
        if case in ("mixed", "mixed-reverse", "unicode"):
            specs = [("fdj.k", "cour"), ("महाराष्ट्र", "DEV"), ("Hello world", "helv")]
            if case == "unicode":
                specs = [specs[1]]
            if case == "mixed-reverse":
                specs.reverse()
            for i, (text, font) in enumerate(specs):
                if font == "DEV":
                    page.insert_font(
                        fontname=font,
                        fontfile=str(
                            Path(__file__).parents[1]
                            / "src/legacylipi/fonts/NotoSansDevanagari-Regular.ttf"
                        ),
                    )
                page.insert_text((40, 70 + i * 40), text, fontname=font)
                if font == "cour":
                    doc.xref_set_key(page.get_fonts()[-1][0], "BaseFont", "/KrutiDev010")
        elif case in ("kruti", "shree"):
            ref = page.insert_font(fontname="cour")
            doc.xref_set_key(
                ref, "BaseFont", "/KrutiDev010" if case == "kruti" else "/ShreeDev0708"
            )
            page.insert_text((40, 70), "fdj.k" if case == "kruti" else "abc 123", fontname="cour")
        elif case != "scanned":
            page.insert_text((40, 70), "Hello world 123", render_mode=3 if case == "overlay" else 0)
        doc.save(path)
    return path


def snapshot(path):
    doc = parse_pdf(path)
    # The original parser returns a set-derived font list. Pin both orders
    # explicitly so the golden is stable across PYTHONHASHSEED values.
    doc.fonts.sort(key=lambda font: font.name, reverse="reverse" in path.stem)
    overall, pages = EncodingDetector().detect_from_document(doc)
    converted = UnicodeConverter().convert_document(doc, page_encodings=pages)
    return (
        asdict(overall),
        {n: asdict(v) for n, v in pages.items()},
        [p.unicode_text for p in converted.pages],
    )


GOLDEN = {
    "latin": (
        {
            "detected_encoding": "unknown",
            "confidence": 0.0,
            "method": DetectionMethod.HEURISTIC,
            "font_name": None,
            "fallback_encodings": [],
        },
        {},
        ["Hello world 123"],
    ),
    "unicode": (
        {
            "detected_encoding": "unicode-devanagari",
            "confidence": 1.0,
            "method": DetectionMethod.UNICODE_DETECTED,
            "font_name": None,
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "unicode-devanagari",
                "confidence": 1.0,
                "method": DetectionMethod.UNICODE_DETECTED,
                "font_name": None,
                "fallback_encodings": [],
            }
        },
        ["महाराष्ट्र"],
    ),
    "kruti": (
        {
            "detected_encoding": "kruti-dev",
            "confidence": 0.95,
            "method": DetectionMethod.FONT_MATCH,
            "font_name": "KrutiDev010",
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "kruti-dev",
                "confidence": 0.95,
                "method": DetectionMethod.FONT_MATCH,
                "font_name": "KrutiDev010",
                "fallback_encodings": [],
            }
        },
        ["किरण"],
    ),
    "shree": (
        {
            "detected_encoding": "shree-dev",
            "confidence": 0.95,
            "method": DetectionMethod.FONT_MATCH,
            "font_name": "ShreeDev0708",
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "shree-dev",
                "confidence": 0.95,
                "method": DetectionMethod.FONT_MATCH,
                "font_name": "ShreeDev0708",
                "fallback_encodings": [],
            }
        },
        ["रलल १२३"],
    ),
    "unused": (
        {
            "detected_encoding": "shree-dev",
            "confidence": 0.95,
            "method": DetectionMethod.FONT_MATCH,
            "font_name": "ShreeDev0708",
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "shree-dev",
                "confidence": 0.95,
                "method": DetectionMethod.FONT_MATCH,
                "font_name": "ShreeDev0708",
                "fallback_encodings": [],
            }
        },
        ["कशश्रश्रेुेीश्रव १२३"],
    ),
    "unused-reverse": (
        {
            "detected_encoding": "shree-dev",
            "confidence": 0.95,
            "method": DetectionMethod.FONT_MATCH,
            "font_name": "ShreeDev0708",
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "shree-dev",
                "confidence": 0.95,
                "method": DetectionMethod.FONT_MATCH,
                "font_name": "ShreeDev0708",
                "fallback_encodings": [],
            }
        },
        ["कशश्रश्रेुेीश्रव १२३"],
    ),
    "mixed": (
        {
            "detected_encoding": "kruti-dev",
            "confidence": 0.95,
            "method": DetectionMethod.FONT_MATCH,
            "font_name": "KrutiDev010",
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "kruti-dev",
                "confidence": 0.95,
                "method": DetectionMethod.FONT_MATCH,
                "font_name": "KrutiDev010",
                "fallback_encodings": [],
            }
        },
        ["किरण\nमहाराष्ट्र\nHमससवूवतसक"],
    ),
    "mixed-reverse": (
        {
            "detected_encoding": "unicode-devanagari",
            "confidence": 1.0,
            "method": DetectionMethod.UNICODE_DETECTED,
            "font_name": None,
            "fallback_encodings": [],
        },
        {
            1: {
                "detected_encoding": "unicode-devanagari",
                "confidence": 1.0,
                "method": DetectionMethod.UNICODE_DETECTED,
                "font_name": None,
                "fallback_encodings": [],
            }
        },
        ["Hello world\nमहाराष्ट्र\nfdj.k"],
    ),
    "overlay": (
        {
            "detected_encoding": "unknown",
            "confidence": 0.0,
            "method": DetectionMethod.HEURISTIC,
            "font_name": None,
            "fallback_encodings": [],
        },
        {},
        ["Hello world 123"],
    ),
    "scanned": (
        {
            "detected_encoding": "unknown",
            "confidence": 0.0,
            "method": DetectionMethod.HEURISTIC,
            "font_name": None,
            "fallback_encodings": [],
        },
        {},
        [""],
    ),
}


@pytest.mark.parametrize("case", CASES)
def test_r4_non_glyph_main_pipeline_golden(tmp_path, case):
    assert snapshot(corpus_pdf(tmp_path / f"{case}.pdf", case)) == GOLDEN[case]


def test_document_with_font_metadata():
    """Test that document-level font metadata is used."""
    detector = EncodingDetector()

    doc = PDFDocument(
        filepath=Path("/test/doc.pdf"),
        pages=[
            PDFPage(
                page_number=1,
                text_blocks=[TextBlock(raw_text="Some text")],
            )
        ],
        fonts=[FontInfo(name="Shree-Dev-0714")],
    )

    overall, page_results = detector.detect_from_document(doc)

    # Should detect from document fonts
    assert overall.detected_encoding == "shree-dev"


@pytest.mark.parametrize("command", ["convert", "extract", "translate"])
def test_r4_forced_encoding_cli(tmp_path, command):
    pdf = corpus_pdf(tmp_path / "legacy.pdf", "kruti")
    output = tmp_path / "out.txt"
    args = [command, str(pdf), "--encoding", "shree-lipi", "-o", str(output)]
    if command == "translate":
        args += ["--translator", "mock", "--format", "text"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    # Main's Shree-Lipi mapping leaves this exact reviewer operand unchanged.
    assert "fdj.k" in output.read_text()
    assert "किरण" not in output.read_text()


def test_r4_forced_encoding_python_api(tmp_path):
    doc = parse_pdf(corpus_pdf(tmp_path / "legacy.pdf", "kruti"))
    override = EncodingDetectionResult("shree-lipi", 1.0, DetectionMethod.USER_OVERRIDE)
    converter = UnicodeConverter()
    assert converter.convert_document(doc, page_encodings={1: override}).unicode_text == "fdj.k"
    assert converter.convert_with_recovery(doc, override, {1: override}).unicode_text == "fdj.k"


def test_r4_same_page_legacy_and_glyph(tmp_path):
    pdf = make_pdf(tmp_path / "mixed.pdf", lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        page = doc[0]
        ref = page.insert_font(fontname="cour")
        doc.xref_set_key(ref, "BaseFont", "/KrutiDev010")
        page.insert_text((40, 120), "fdj.k", fontname="cour")
        doc.saveIncr()
    doc = parse_pdf(pdf)
    overall, pages = EncodingDetector().detect_from_document(doc)
    result = UnicodeConverter().convert_with_recovery(doc, overall, pages)
    assert result.unicode_text == "తెలుగు\nकिरण"
