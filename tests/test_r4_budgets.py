"""Used-font and diagnostic resource regressions, all fixtures generated offline."""

import base64
import os
import zlib

import fitz
import pytest

import legacylipi.core.glyph_recovery as gr
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.pdf_parser import parse_pdf
from legacylipi.core.unicode_converter import UnicodeConverter
from tests.glyph_helpers import make_pdf


def encoded_font(path, filter_name, used=True):
    pdf = make_pdf(path, lines=["తెలుగు"] if used else [])
    with fitz.open(pdf) as doc:
        data = doc.xref_stream(8)
        if filter_name == "ASCIIHexDecode":
            encoded = data.hex().encode() + b">"
        elif filter_name == "ASCII85Decode":
            encoded = base64.a85encode(data) + b"~>"
        elif filter_name == "FlateDecode":
            encoded = zlib.compress(data)
        elif filter_name == "LZWDecode":
            # Literal LZW codes with frequent clear: 9 bits throughout.
            codes = []
            for start in range(0, len(data), 200):
                codes.extend([256, *data[start : start + 200]])
            codes.append(257)
            bits = "".join(f"{code:09b}" for code in codes)
            bits += "0" * (-len(bits) % 8)
            encoded = int(bits, 2).to_bytes(len(bits) // 8, "big")
        else:
            encoded = data
        doc.update_stream(8, encoded, compress=False)
        doc.xref_set_key(8, "Filter", "/" + filter_name)
        if not used:
            doc[0].insert_text((40, 70), "Hello plain Latin")
        doc.saveIncr()
    return pdf


@pytest.mark.parametrize(
    "filter_name",
    ["ASCIIHexDecode", "ASCII85Decode", "FlateDecode", "LZWDecode", "UnsupportedDecode"],
)
def test_r4_unused_font_never_decoded(tmp_path, filter_name, monkeypatch):
    pdf = encoded_font(tmp_path / "unused.pdf", filter_name, used=False)
    original = gr._Budget.stream

    def tracked(self, doc, xref, **kwargs):
        assert xref not in (8, 12), "unused font program was decoded"
        return original(self, doc, xref, **kwargs)

    monkeypatch.setattr(gr._Budget, "stream", tracked)
    doc = parse_pdf(pdf)
    overall, pages = EncodingDetector().detect_from_document(doc)
    assert (
        UnicodeConverter().convert_with_recovery(doc, overall, pages).unicode_text
        == "Hello plain Latin"
    )


@pytest.mark.parametrize(
    "filter_name", ["ASCIIHexDecode", "ASCII85Decode", "FlateDecode", "LZWDecode"]
)
def test_r4_supported_font_filters(tmp_path, filter_name):
    pdf = encoded_font(tmp_path / "used.pdf", filter_name)
    assert gr.recover_pdf(pdf).text.strip() == "తెలుగు"


def test_r4_unsupported_used_candidate_is_excluded(tmp_path):
    pdf = encoded_font(tmp_path / "unsupported.pdf", "UnsupportedDecode")
    assert gr.find_glyph_id_fonts(pdf) == []
    with pytest.raises(ValueError, match="NotoSansTelugu"):
        gr.recover_pdf(pdf)


def duplicate_pdf(path, source, count, shared=False):
    with fitz.open(source) as src, fitz.open() as dst:
        dst.insert_pdf(src)
        for _ in range(count - 1):
            if shared:
                dst.fullcopy_page(0)
            else:
                dst.insert_pdf(src)
        dst.save(path)
    return path


def test_r4_font_budget_counts_used_hashes(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "one.pdf", lines=["తెలుగు"])
    pdf = duplicate_pdf(tmp_path / "many.pdf", src, 12)
    monkeypatch.setattr(gr, "MAX_CANDIDATE_FONTS", 1)
    calls = []
    original = gr.build_glyph_map

    def tracked(data, *args, **kwargs):
        calls.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(gr, "build_glyph_map", tracked)
    result = gr.recover_pdf(pdf)
    assert len(result.pages) == 12
    assert all(page.unicode_text == "తెలుగు" for page in result.pages)
    assert len(calls) == 1
    assert len(result.report["fonts"]) == 1


@pytest.mark.slow
@pytest.mark.skipif(bool(os.environ.get("CI")), reason="generated 200-page resource check")
def test_r4_200_copied_font_pages(tmp_path):
    src = make_pdf(tmp_path / "one.pdf", lines=["తెలుగు భాష " * 6] * 20)
    result = gr.recover_pdf(duplicate_pdf(tmp_path / "200.pdf", src, 200))
    assert len(result.pages) == 200
    assert len(result.report["fonts"]) == 1
    assert result.report["unknown_count"] == 0


def test_r4_diagnostic_budget_is_incomplete_not_fatal(tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "joiner.pdf", lines=["క్\u200cక " * 20] * 20)
    with fitz.open(src) as doc:
        doc.xref_set_key(6, "W", "[3 [0]]")
        doc.saveIncr()
    monkeypatch.setattr(gr, "MAX_ALIGNMENT_WORK", 1)
    result = gr.recover_pdf(src)
    assert result.report["unknown_count"] == 0
    assert result.report["verification_complete"] is False
    assert result.report["roundtrip_match_ratio"] is None
    assert any(w["code"] == "verification-incomplete" for w in result.warnings)
    assert all(not r["verification_complete"] for p in result.report["pages"] for r in p["runs"])


@pytest.mark.slow
@pytest.mark.skipif(bool(os.environ.get("CI")), reason="reviewer's generated 200-page joiner check")
def test_r4_200_page_zwnj_diagnostic(tmp_path):
    src = make_pdf(tmp_path / "joiner.pdf", lines=["క్\u200cక " * 20] * 20)
    with fitz.open(src) as doc:
        doc.xref_set_key(6, "W", "[3 [0]]")
        doc.saveIncr()
    result = gr.recover_pdf(duplicate_pdf(tmp_path / "200.pdf", src, 200, shared=True))
    assert len(result.pages) == 200
    assert result.report["unknown_count"] == 0
    assert result.report["verification_complete"] is False
    assert any(w["code"] == "verification-incomplete" for w in result.warnings)


def test_r4_duplicate_fonts_share_decoded_byte_allowance(tmp_path, monkeypatch):
    src = encoded_font(tmp_path / "compressed.pdf", "FlateDecode")
    pdf = duplicate_pdf(tmp_path / "copies.pdf", src, 3)
    with fitz.open(src) as doc:
        size = len(doc.xref_stream(8))
    monkeypatch.setattr(gr, "MAX_TOTAL_BYTES", size + 10_000)
    result = gr.recover_pdf(pdf)
    assert len(result.pages) == 3
