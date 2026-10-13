"""Exercise actual detection, recovery, mock translation and output offline."""

import sys
from unittest.mock import Mock

import fitz
import pytest
from click.testing import CliRunner
from tests.glyph_helpers import FONT, LINES, make_pdf

from legacylipi.api.pipeline import run_translate
from legacylipi.api.schemas import TranslateRequest
from legacylipi.cli import main
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.models import DetectionMethod, EncodingDetectionResult
from legacylipi.core.output_generator import OutputGenerator
from legacylipi.core.pdf_parser import parse_pdf
from legacylipi.core.translator import MockTranslationBackend
from legacylipi.core.unicode_converter import UnicodeConverter


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "telugu.pdf")


@pytest.fixture
def translation_calls(monkeypatch):
    calls = []
    original = MockTranslationBackend.translate

    async def recorded(self, text, source_lang, target_lang):
        calls.append((text, source_lang, target_lang))
        return await original(self, text, source_lang, target_lang)

    monkeypatch.setattr(MockTranslationBackend, "translate", recorded)
    return calls


@pytest.mark.parametrize("fmt", ["text", "markdown"])
@pytest.mark.parametrize("source_lang", [None, "hi"])
def test_translate_cli(pdf, tmp_path, translation_calls, fmt, source_lang):
    out = tmp_path / ("out.md" if fmt == "markdown" else "out.txt")
    args = ["translate", str(pdf), "--translator", "mock", "--format", fmt, "-o", str(out)]
    if source_lang:
        args += ["--source-lang", source_lang]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert translation_calls == [("\n".join(LINES), source_lang or "te", "en")]
    assert out.exists() and LINES[0] in out.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["text", "markdown"])
@pytest.mark.parametrize("source_lang", [None, "hi"])
async def test_translate_api(pdf, translation_calls, fmt, source_lang):
    content, filename = await run_translate(
        pdf.read_bytes(),
        pdf.name,
        TranslateRequest(translator="mock", output_format=fmt, source_lang=source_lang),
    )
    assert translation_calls == [("\n".join(LINES), source_lang or "te", "en")]
    assert LINES[0] in content.decode()
    assert filename.endswith(".txt" if fmt == "text" else ".md")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["api", "cli"])
async def test_positioned_pdf(pdf, tmp_path, translation_calls, monkeypatch, entry):
    # Deterministic rendering uses the same licensed font on every platform.
    monkeypatch.setattr(OutputGenerator, "_get_unicode_font", lambda *a, **kw: str(FONT))
    placed = []
    original = OutputGenerator._place_translated_blocks_with_positions

    def recorded(self, page, blocks, font_path, **kwargs):
        placed.extend(blocks)
        return original(self, page, blocks, font_path, **kwargs)

    monkeypatch.setattr(OutputGenerator, "_place_translated_blocks_with_positions", recorded)
    if entry == "api":
        content, _ = await run_translate(
            pdf.read_bytes(),
            pdf.name,
            TranslateRequest(translator="mock", output_format="pdf"),
        )
    else:
        # Click's pipeline owns its event loop.
        import asyncio

        out = tmp_path / "out.pdf"
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            [
                "translate",
                str(pdf),
                "--translator",
                "mock",
                "--format",
                "pdf",
                "--structure-preserving",
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        content = out.read_bytes()
    assert [call[0] for call in translation_calls] == LINES
    assert all(call[1] == "te" for call in translation_calls)
    assert [block.unicode_text for block in placed] == LINES
    assert all(block.position.width > 0 and block.position.height > 0 for block in placed)
    assert all(block.font_name and block.font_size == 14 for block in placed)
    with fitz.open(stream=content, filetype="pdf") as output:
        assert len(output) == 2  # Existing metadata cover + one source page.
        assert output[1].rect == fitz.Rect(0, 0, 900, 800)
        assert output[1].get_text().strip()
        assert output[1].get_text("blocks")[0][0] == pytest.approx(40, abs=2)


def test_recovered_document_selected_pages(pdf):
    from legacylipi.core.glyph_recovery import recover_document

    with fitz.open(pdf) as source:
        source.new_page().insert_text((40, 50), "Ordinary Unicode page")
        source.saveIncr()
    document = parse_pdf(pdf)
    result, pages = EncodingDetector().detect_from_document(document)
    recovered = recover_document(document, set(pages))
    assert recovered.pages[0].unicode_text == "\n".join(LINES)
    assert recovered.pages[1] is document.pages[1]
    assert recovered.metadata is document.metadata
    assert recovered.fonts is document.fonts
    assert document.pages[0].unicode_text != recovered.pages[0].unicode_text
    converter = UnicodeConverter()
    converted = converter.convert_with_recovery(document, result, pages)
    assert converted.pages[0].unicode_text == recovered.pages[0].unicode_text
    assert converted.pages[1] == converter.convert_page(document.pages[1])


@pytest.mark.parametrize("encoding", ["shree-dev", "kruti-dev"])
def test_legacy_delegates(pdf, encoding):
    document = parse_pdf(pdf)
    result = EncodingDetectionResult(encoding, 1, DetectionMethod.FONT_MATCH)
    pages = {1: result}
    converter = UnicodeConverter()
    converter.convert_document = Mock(return_value=document)
    assert converter.convert_with_recovery(document, result, pages) is document
    converter.convert_document.assert_called_once_with(document, page_encodings=pages)


@pytest.mark.parametrize("dependency", ["uharfbuzz", "fontTools.ttLib"])
def test_missing_extra(pdf, monkeypatch, dependency):
    document = parse_pdf(pdf)
    result, pages = EncodingDetector().detect_from_document(document)
    monkeypatch.setitem(sys.modules, dependency, None)
    with pytest.raises(ImportError, match=r"legacylipi\[glyph\]"):
        UnicodeConverter().convert_with_recovery(document, result, pages)


def test_missing_extra_cli(pdf, monkeypatch):
    monkeypatch.setitem(sys.modules, "uharfbuzz", None)
    result = CliRunner().invoke(main, ["translate", str(pdf), "--translator", "mock"])
    assert result.exit_code != 0
    assert "legacylipi[glyph]" in result.output


@pytest.mark.parametrize("command", ["convert", "extract"])
def test_other_cli_conversion(pdf, tmp_path, command):
    out = tmp_path / "out.txt"
    result = CliRunner().invoke(main, [command, str(pdf), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert LINES[0] in out.read_text()


def test_telugu_output_font(monkeypatch):
    from pathlib import Path

    # A bundled Devanagari font must not mask an installed Telugu font.
    telugu = "/System/Library/Fonts/KohinoorTelugu.ttc"
    monkeypatch.setattr(Path, "exists", lambda p: str(p) == telugu or "Devanagari" in str(p))
    assert OutputGenerator()._get_unicode_font("te") == telugu
