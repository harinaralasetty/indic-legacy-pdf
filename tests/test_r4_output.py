"""Exact logical extraction and independent shaping regressions for review R4."""

import io
import re
from pathlib import Path

import fitz
import pytest
from pypdf import PdfReader

from legacylipi.core.output_generator import OutputGenerator
from tests.glyph_helpers import FONT, shape


@pytest.mark.parametrize(
    "text",
    [
        "తెలుగు",
        "జ్ఞానము",
        "స్త్రీ క్ష్మ్య స్త్వం శ్రద్ధ క్రై త్రై",
        "తెలుగు ABC 123",
        "తెలుగు  రెండు",
        "తెలుగు, భాష! (2025).",
        "క్షేత్రం కృష్ణా ప్రక్రియ అమ్మ ౧౨౩",
        "తెలుగు\nజ్ఞానము\nABC 123 తెలుగు!",
    ],
)
def test_r4_output_exact_logical_text_and_gids(text):
    with fitz.open() as doc:
        page = doc.new_page(width=1000)
        for i, line in enumerate(text.splitlines()):
            OutputGenerator()._insert_text_with_font(page, 40, 70 + i * 30, line, 14, str(FONT))
        data = doc.tobytes()
    with fitz.open(stream=data, filetype="pdf") as doc:
        assert doc[0].get_text().rstrip("\n") == text
        # Multi-codepoint ToUnicode expansions use synthetic GID -1 entries.
        gids = [c[1] for s in doc[0].get_texttrace() for c in s["chars"] if c[1] >= 0]
        fallback = Path(__file__).parents[1] / "src/legacylipi/fonts/NotoSansDevanagari-Regular.ttf"
        expected = []
        for line in text.splitlines():
            for part in re.split(r"([A-Za-z]+)", line):
                if part:
                    expected.extend(
                        shape(
                            part,
                            (fallback if part.isascii() and part.isalpha() else FONT).read_bytes(),
                        )
                    )
        assert gids == expected
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text().rstrip("\n") == text


def test_r4_latin_mix_has_no_missing_glyph_boxes():
    with fitz.open() as doc:
        page = doc.new_page()
        OutputGenerator()._insert_text_with_font(page, 40, 70, "తెలుగు ABC", 14, str(FONT))
        assert all(c[1] != 0 for s in page.get_texttrace() for c in s["chars"])


@pytest.mark.parametrize("text", ["తెలుగు", "జ్ఞానము", "స్త్రీ క్ష్మ్య శ్రద్ధ", "కై కౌ", "తెలుగు ABC 123!", "ి"])
def test_r4_platform_font_exact_text_and_positions(text):
    import uharfbuzz as hb

    path = OutputGenerator()._get_unicode_font("te")
    if path is None:
        pytest.skip("No installed Telugu font")
    with fitz.open() as doc:
        page = doc.new_page()
        OutputGenerator()._insert_text_with_font(page, 40, 70, text, 14, path)
        data = doc.tobytes()
    font = hb.Font(hb.Face(Path(path).read_bytes()))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    expected_positions = []
    x = 40
    for position in buf.glyph_positions:
        expected_positions.append(
            (
                x + position.x_offset * 14 / font.face.upem,
                70 - position.y_offset * 14 / font.face.upem,
            )
        )
        x += position.x_advance * 14 / font.face.upem
    with fitz.open(stream=data, filetype="pdf") as doc:
        assert doc[0].get_text().rstrip("\n") == text
        painted = [c for s in doc[0].get_texttrace() for c in s["chars"] if c[1] >= 0]
        assert [c[1] for c in painted] == [g.codepoint for g in buf.glyph_infos]
        for char, expected in zip(painted, expected_positions, strict=True):
            assert char[2] == pytest.approx(expected, abs=0.001)
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text().rstrip("\n") == text
