"""Main's PDF writer remains usable without the optional recovery extra."""

import sys
from pathlib import Path

import fitz
import pytest
from click.testing import CliRunner

from legacylipi.api.pipeline import run_translate
from legacylipi.api.schemas import TranslateRequest
from legacylipi.cli import main
from legacylipi.core.models import (
    DetectionMethod,
    EncodingDetectionResult,
    PDFDocument,
    PDFPage,
    TextBlock,
)
from legacylipi.core.output_generator import OutputGenerator
from tests.glyph_helpers import FONT

BUNDLED = Path(__file__).parents[1] / "src/legacylipi/fonts/NotoSansDevanagari-Regular.ttf"


def unicode_pdf(path, text):
    with fitz.open() as doc:
        page = doc.new_page()
        writer = fitz.TextWriter(page.rect)
        writer.append((40, 60), text, font=fitz.Font(fontfile=str(FONT)), fontsize=14)
        writer.write_text(page)
        doc.save(path)
    return path


@pytest.mark.parametrize("text", ["తెలుగు", "తెలుగు किरण", "Hello Latin"])
def test_pdf_writer_matches_main_textwriter(text):
    with fitz.open() as expected, fitz.open() as actual:
        reference = expected.new_page()
        writer = fitz.TextWriter(reference.rect)
        writer.append((40, 60), text, font=fitz.Font(fontfile=str(BUNDLED)), fontsize=14)
        writer.write_text(reference, color=(0, 0, 0))
        page = actual.new_page()
        OutputGenerator()._insert_text_with_font(page, 40, 60, text, 14, str(BUNDLED))
        assert page.get_pixmap().samples == reference.get_pixmap().samples
        assert page.get_texttrace() == reference.get_texttrace()


def test_glyph_encoding_does_not_change_output_font():
    doc = PDFDocument(Path("input.pdf"), [PDFPage(1, [TextBlock(raw_text="తెలుగు किरण")])])
    output = []
    for encoding in ["unicode", "glyph-id"]:
        data = OutputGenerator(include_metadata=False).generate_pdf(
            doc, EncodingDetectionResult(encoding, 1, DetectionMethod.FONT_MATCH)
        )
        with fitz.open(stream=data, filetype="pdf") as pdf:
            output.append(pdf[0].get_pixmap().samples)
    assert output[0] == output[1]


@pytest.mark.parametrize("text", ["తెలుగు", "Hello Latin"])
@pytest.mark.parametrize("fmt", ["pdf", "text", "markdown"])
def test_cli_output_warning_and_missing_extra(tmp_path, monkeypatch, text, fmt):
    pdf = unicode_pdf(tmp_path / "unicode.pdf", text)
    for module in ["uharfbuzz", "fontTools", "fontTools.ttLib"]:
        monkeypatch.setitem(sys.modules, module, None)
    result = CliRunner().invoke(
        main, ["extract", str(pdf), "--format", fmt, "--quiet", "-o", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    needs_warning = text == "తెలుగు" and fmt == "pdf"
    assert ("does not shape Indic scripts" in result.output) == needs_warning
    if needs_warning:
        assert "Warning" in result.output and "text/markdown" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["pdf", "text", "markdown"])
@pytest.mark.parametrize("mode", ["structure_preserving", "flowing"])
async def test_api_pdf_warning(tmp_path, fmt, mode):
    pdf = unicode_pdf(tmp_path / "unicode.pdf", "తెలుగు")
    result = await run_translate(
        pdf.read_bytes(),
        pdf.name,
        TranslateRequest(
            translator="mock", output_format=fmt, translation_mode=mode, target_lang="te"
        ),
    )
    warnings = [w for w in result.warnings if w["code"] == "pdf-indic-unshaped"]
    assert bool(warnings) == (fmt == "pdf")
    if warnings:
        assert len(warnings) == 1
        assert "does not shape Indic scripts" in warnings[0]["message"]
        assert "text/markdown" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_api_ocr_conversion_pdf_warning(tmp_path, monkeypatch):
    from legacylipi.api.pipeline import run_convert
    from legacylipi.api.schemas import ConvertRequest
    from legacylipi.core.pdf_parser import parse_pdf

    pdf = unicode_pdf(tmp_path / "unicode.pdf", "తెలుగు")
    # OCR is the external boundary; exercise actual output and diagnostics.
    monkeypatch.setattr(
        "legacylipi.core.ocr_parser.parse_pdf_with_ocr", lambda *a, **kw: parse_pdf(pdf)
    )
    result = await run_convert(
        pdf.read_bytes(), pdf.name, ConvertRequest(output_format="pdf", ocr_engine="tesseract")
    )
    assert result.content.startswith(b"%PDF")
    assert [w["code"] for w in result.warnings] == ["pdf-indic-unshaped"]


@pytest.mark.parametrize("text,expected", [("తెలుగు", True), ("Hello Latin", False)])
def test_cli_translated_pdf_warning(tmp_path, text, expected):
    pdf = unicode_pdf(tmp_path / "unicode.pdf", text)
    result = CliRunner().invoke(
        main,
        [
            "translate",
            str(pdf),
            "--translator",
            "mock",
            "--format",
            "pdf",
            "--quiet",
            "-o",
            str(tmp_path / "out.pdf"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert ("does not shape Indic scripts" in result.output) == expected


@pytest.mark.asyncio
async def test_english_translation_does_not_warn_for_telugu_source(tmp_path, monkeypatch):
    from legacylipi.core.translator import MockTranslationBackend

    async def english(self, text, source_lang, target_lang):
        return "English translation"

    monkeypatch.setattr(MockTranslationBackend, "translate", english)
    pdf = unicode_pdf(tmp_path / "unicode.pdf", "తెలుగు")
    result = await run_translate(
        pdf.read_bytes(), pdf.name, TranslateRequest(translator="mock", output_format="pdf")
    )
    assert not any(w["code"] == "pdf-indic-unshaped" for w in result.warnings)
