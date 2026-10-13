"""Ground truth comes from Unicode + shaping, never from the decoder's own map."""

import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import fitz
import pytest
import uharfbuzz as hb
from click.testing import CliRunner
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from legacylipi.cli import main
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.glyph_recovery import (
    GlyphMap,
    build_glyph_map,
    decode_glyph_stream,
    find_glyph_id_fonts,
    recover_pdf,
    verify_roundtrip,
)
from legacylipi.core.pdf_parser import parse_pdf

FONT = Path(__file__).parent / "fixtures/NotoSansTelugu-Regular.ttf"
LINES = ["తెలుగు విశాఖపట్నం ప్రభుత్వం స్త్రీ", "క్షేత్రం కృష్ణా ప్రక్రియ అమ్మ 2025"]


def shape(text, data):
    font = hb.Font(hb.Face(data))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    return [g.codepoint for g in buf.glyph_infos]


def make_pdf(
    path,
    *,
    lines=None,
    font_data=None,
    second_font_data=None,
    to_unicode=False,
    embedded=True,
    multiple_fonts=False,
    literal=False,
    cid_map=False,
    font_name="NotoSansTelugu-Regular",
):
    """Handwritten Identity-H PDF; arbitrary CID remapping tests CID != GID."""
    data = font_data or FONT.read_bytes()
    lines = LINES if lines is None else lines
    objects = []

    def add(value):
        objects.append(value)
        return len(objects)

    def stream(value, extra=b""):
        return (
            b"<< /Length "
            + str(len(value)).encode()
            + b" "
            + extra
            + b" >>\nstream\n"
            + value
            + b"\nendstream"
        )

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 800] /Resources << /Font << /F1 5 0 R /F2 9 0 R /F3 13 0 R >> >> /Contents 4 0 R >>"
    )
    content = []
    for i, text in enumerate(lines):
        gids = shape(
            text, second_font_data if second_font_data and multiple_fonts and i % 2 else data
        )
        values = [(g + 1 if cid_map else g).to_bytes(2, "big") for g in gids]
        raw = b"".join(values)
        operand = (
            (b"(" + b"".join(b"\\%03o" % b for b in raw) + b")")
            if literal
            else b"<" + raw.hex().encode() + b">"
        )
        resource = b"F2" if multiple_fonts and i % 2 else b"F1"
        content.append(
            b"BT /"
            + resource
            + b" 14 Tf 1 0 0 1 40 "
            + str(740 - 30 * i).encode()
            + b" Tm "
            + operand
            + b" Tj ET"
        )
    if multiple_fonts:
        content.append(b"BT /F3 14 Tf 1 0 0 1 40 650 Tm (Latin 123) Tj ET")
    add(stream(b"\n".join(content)))
    for start in (5, 9):
        cmap = b" /ToUnicode 14 0 R" if to_unicode else b""
        add(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /NotoSansTelugu-Regular /Encoding /Identity-H /DescendantFonts ["
            + str(start + 1).encode()
            + b" 0 R]"
            + cmap
            + b" >>"
        )
        mapping = b"15 0 R" if cid_map else b"/Identity"
        add(
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NotoSansTelugu-Regular /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 600 /CIDToGIDMap "
            + mapping
            + b" /FontDescriptor "
            + str(start + 2).encode()
            + b" 0 R >>"
        )
        file_ref = b" /FontFile2 " + str(start + 3).encode() + b" 0 R" if embedded else b""
        add(
            b"<< /Type /FontDescriptor /FontName /NotoSansTelugu-Regular /Flags 4 /FontBBox [-100 -500 2000 1500] /ItalicAngle 0 /Ascent 1000 /Descent -500 /CapHeight 700 /StemV 80"
            + file_ref
            + b" >>"
        )
        add(stream(second_font_data if start == 9 and second_font_data else data))
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add(
        stream(
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Test def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange 1 beginbfchar <0001> <0041> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
        )
    )
    add(
        stream(
            b"\0\0"
            + b"".join(
                g.to_bytes(2, "big") for g in range(len(TTFont(io.BytesIO(data)).getGlyphOrder()))
            )
        )
    )
    out = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        if b"stream\n" not in obj:
            obj = obj.replace(b"NotoSansTelugu-Regular", font_name.encode())
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(out)
    return path


@pytest.fixture
def latin_font_data():
    """A genuinely Latin-only sfnt, built offline without the Telugu fixture."""
    builder = FontBuilder(1000, isTTF=True)
    cmap = {cp: f"uni{cp:04X}" for cp in range(32, 127)}
    names = [".notdef", *cmap.values()]
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        if name != "uni0020":
            pen.moveTo((50, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(dict.fromkeys(names, (600, 0)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "LatinFixture", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    out = io.BytesIO()
    builder.save(out)
    return out.getvalue()


def baseline_detection(pdf, tmp_path):
    """The same parsed content, with the file-only glyph override disabled."""
    document = parse_pdf(pdf)
    return EncodingDetector().detect_from_document(
        replace(document, filepath=tmp_path / "no-file.pdf")
    )


@pytest.mark.parametrize("encoding", ["Identity-H", "Identity-V"])
def test_latin_identity_not_candidate(tmp_path, latin_font_data, encoding):
    pdf = make_pdf(
        tmp_path / "latin.pdf", font_data=latin_font_data, font_name="LatinFixture", lines=["ABC"]
    )
    if encoding == "Identity-V":
        with fitz.open(pdf) as doc:
            doc.xref_set_key(5, "Encoding", "/Identity-V")
            doc.saveIncr()
    with TTFont(io.BytesIO(latin_font_data)) as font:
        assert not any(0x0C00 <= cp <= 0x0C7F for cp in font.getBestCmap())
        assert "GSUB" not in font and "GPOS" not in font
    assert find_glyph_id_fonts(pdf) == []


def test_latin_identity_preserves_detection(tmp_path, latin_font_data):
    pdf = make_pdf(
        tmp_path / "latin.pdf", font_data=latin_font_data, font_name="LatinFixture", lines=["ABC"]
    )
    assert EncodingDetector().detect_from_document(parse_pdf(pdf)) == baseline_detection(
        pdf, tmp_path
    )


@pytest.mark.parametrize("embedded_script", ["latin", "telugu"])
def test_legacy_signal_keeps_precedence(tmp_path, latin_font_data, embedded_script):
    pdf = make_pdf(
        tmp_path / "mixed.pdf",
        font_data=latin_font_data if embedded_script == "latin" else FONT.read_bytes(),
        font_name="LatinFixture" if embedded_script == "latin" else "NotoSansTelugu-Regular",
        lines=["ABC"] if embedded_script == "latin" else [LINES[0]],
        multiple_fonts=True,
    )
    with fitz.open(pdf) as doc:
        doc.xref_set_key(13, "BaseFont", "/KrutiDev010")
        doc.saveIncr()
    expected = baseline_detection(pdf, tmp_path)
    assert expected[0].detected_encoding == "kruti-dev"
    assert EncodingDetector().detect_from_document(parse_pdf(pdf)) == expected


def test_unused_telugu_resources_do_not_override(tmp_path):
    pdf = make_pdf(tmp_path / "unused.pdf", lines=[], multiple_fonts=True)
    assert find_glyph_id_fonts(pdf) == []
    assert EncodingDetector().detect_from_document(parse_pdf(pdf)) == baseline_detection(
        pdf, tmp_path
    )


def test_only_pages_drawing_telugu_are_flagged(tmp_path):
    pdf = make_pdf(tmp_path / "telugu.pdf", lines=[LINES[0]])
    with fitz.open(pdf) as doc:
        page = doc.new_page()
        doc.xref_set_key(page.xref, "Resources", doc.xref_get_key(doc[0].xref, "Resources")[1])
        doc.saveIncr()
    fonts = find_glyph_id_fonts(pdf)
    assert [(font.xref, font.pages) for font in fonts] == [(5, [1])]
    overall, pages = EncodingDetector().detect_from_document(parse_pdf(pdf))
    assert overall.detected_encoding == "glyph-id"
    assert set(pages) == {1}


def test_fonttools_missing_means_unverified(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "telugu.pdf")
    assert find_glyph_id_fonts(pdf)  # Also exercise the warmed verdict cache.
    monkeypatch.setitem(sys.modules, "fontTools.ttLib", None)
    assert find_glyph_id_fonts(pdf) == []


def test_font_verdict_cached_by_content(tmp_path, monkeypatch):
    import fontTools.ttLib

    font = TTFont(FONT)
    font["name"].setName("Cache regression fixture", 1, 3, 1, 0x409)
    out = io.BytesIO()
    font.save(out)
    pdf = make_pdf(tmp_path / "cached.pdf", font_data=out.getvalue(), multiple_fonts=True)
    calls = []

    def counted_font(*args, **kwargs):
        calls.append(1)
        return TTFont(*args, **kwargs)

    monkeypatch.setattr(fontTools.ttLib, "TTFont", counted_font)
    assert find_glyph_id_fonts(pdf)
    assert find_glyph_id_fonts(pdf)
    assert len(calls) == 1  # Identical programs at two xrefs, across two scans.


@pytest.mark.parametrize("evidence", ["cmap", "unicode-name", "telu-name", "GSUB", "GPOS"])
def test_telugu_font_evidence(tmp_path, latin_font_data, evidence):
    """Each accepted font-program signal is sufficient on its own."""
    from fontTools.feaLib.builder import addOpenTypeFeaturesFromString

    font = TTFont(io.BytesIO(latin_font_data))
    if evidence == "cmap":
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[0x0C15] = "uni0041"
    elif evidence in ("unicode-name", "telu-name"):
        font["post"].mapping["uni0041"] = "uni0C15" if evidence == "unicode-name" else "katelu"
    else:
        feature = (
            "languagesystem tel2 dflt; feature liga { sub uni0041 by uni0042; } liga;"
            if evidence == "GSUB"
            else "languagesystem telu dflt; feature kern { pos uni0041 uni0042 -20; } kern;"
        )
        addOpenTypeFeaturesFromString(font, feature)
    out = io.BytesIO()
    font.save(out)
    pdf = make_pdf(tmp_path / "evidence.pdf", font_data=out.getvalue(), lines=["ABC"])
    assert find_glyph_id_fonts(pdf)


def test_report_and_cli_explain_internal_consistency(tmp_path):
    pdf = make_pdf(tmp_path / "telugu.pdf")
    report = tmp_path / "report.json"
    run = CliRunner().invoke(
        main, ["recover-text", str(pdf), "-o", str(tmp_path / "out.txt"), "--report", str(report)]
    )
    assert run.exit_code == 0, run.output
    note = json.loads(report.read_text())["verification_note"]
    assert "internal consistency of reading order/whitespace/ambiguity" in note
    assert "does NOT verify that inferred cmap mappings are correct" in note
    assert "Gautami sample only" in note
    assert " ".join(note.split()) in " ".join(run.output.split())


@pytest.fixture(scope="module")
def glyph_map():
    return build_glyph_map(FONT.read_bytes())


def test_shape_decode_ground_truth(glyph_map):
    for text in LINES + ["స్త్రై క్క్షౌ అం అః కఁ ౧౨౩ । ॥"]:
        gids = shape(text, FONT.read_bytes())
        assert 0 not in gids
        result = decode_glyph_stream(gids, glyph_map)
        assert result.text == text
        assert result.unknown_count == 0
        assert result.total_glyphs == len(gids)
        assert verify_roundtrip(result.text, gids, glyph_map).match_ratio == 1


@pytest.mark.parametrize(
    "kwargs", [{}, {"literal": True}, {"multiple_fonts": True}, {"cid_map": True}]
)
def test_recover_pdf(tmp_path, kwargs):
    pdf = make_pdf(tmp_path / "synthetic.pdf", **kwargs)
    fonts = find_glyph_id_fonts(pdf)
    assert fonts and fonts[0].pages == [1]
    result = recover_pdf(pdf)
    expected = "\n".join(LINES) + ("\nLatin 123" if kwargs.get("multiple_fonts") else "")
    assert result.text.strip() == expected
    assert result.report["unknown_count"] == 0
    assert result.report["roundtrip_match_ratio"] == 1
    assert result.report["well_formedness_ratio"] == 1


@pytest.mark.parametrize("kwargs", [{"to_unicode": True}, {"embedded": False}])
def test_non_candidates(tmp_path, kwargs):
    assert find_glyph_id_fonts(make_pdf(tmp_path / "other.pdf", **kwargs)) == []


def test_collision_and_unknown():
    mapping = GlyphMap()
    mapping.add((3,), "\u00a0")
    mapping.add((3,), " ")
    mapping.add((9,), "క్క")
    mapping.add((9,), "క")
    mapping.add((9, 10), "కి")
    assert len(mapping.collisions) == 2
    assert decode_glyph_stream([3, 9, 10, 65535], mapping).text == " కి\ufffd"
    assert decode_glyph_stream([65535], mapping).unknown_count == 1
    assert decode_glyph_stream([], mapping).total_glyphs == 0


def test_cache_and_script(glyph_map):
    assert build_glyph_map(FONT.read_bytes()) is glyph_map
    with pytest.raises(ValueError, match="script"):
        build_glyph_map(FONT.read_bytes(), script="unsupported")


def test_trimmed_cmap_repair():
    font = TTFont(FONT)
    for cmap in font["cmap"].tables:
        if cmap.isUnicode():
            for cp in (0x0C4D, 0x0C3F, 0x0C40):
                cmap.cmap.pop(cp, None)
    out = io.BytesIO()
    font.save(out)
    repaired = build_glyph_map(out.getvalue())
    text = "స్త్రీ కృష్ణా"
    assert decode_glyph_stream(shape(text, FONT.read_bytes()), repaired).text == text
    assert repaired.cmap_repairs


def test_roundtrip_detects_wrong_text(glyph_map):
    result = verify_roundtrip("తలుగు", shape("తెలుగు", FONT.read_bytes()), glyph_map)
    assert result.match_ratio < 1
    assert result.mismatching_spans
    malformed = verify_roundtrip("ిక \ufffd", [65535], glyph_map)
    assert malformed.well_formedness_ratio < 1
    assert malformed.well_formedness_issues


def test_cli_and_detection(tmp_path):
    from legacylipi.core.encoding_detector import EncodingDetector
    from legacylipi.core.pdf_parser import parse_pdf

    pdf = make_pdf(tmp_path / "synthetic.pdf")
    result, pages = EncodingDetector().detect_from_document(parse_pdf(pdf))
    assert result.detected_encoding == "glyph-id"
    assert pages[1].detected_encoding == "glyph-id"
    runner = CliRunner()
    detected = runner.invoke(main, ["detect", str(pdf)])
    assert detected.exit_code == 0
    assert "recover-text" in detected.output
    out = tmp_path / "out.txt"
    report = tmp_path / "report.json"
    run = runner.invoke(main, ["recover-text", str(pdf), "-o", str(out), "--report", str(report)])
    assert run.exit_code == 0, run.output
    assert out.read_text().strip() == "\n".join(LINES)
    assert json.loads(report.read_text())["roundtrip_match_ratio"] == 1
    other = make_pdf(tmp_path / "normal.pdf", to_unicode=True)
    assert runner.invoke(main, ["recover-text", str(other), "-o", str(out)]).exit_code != 0


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("LEGACYLIPI_TELUGU_SAMPLES"), reason="local private samples opt-in"
)
def test_real_samples():
    paths = sorted(Path(os.environ["LEGACYLIPI_TELUGU_SAMPLES"]).glob("*.pdf"))
    assert paths
    for path in paths:
        assert find_glyph_id_fonts(path)
        result = recover_pdf(path)
        assert result.report["unknown_count"] / result.report["total_glyphs"] < 0.01
        assert result.report["roundtrip_match_ratio"] >= 0.99


def test_nameless_cmap_repair_records_inference():
    """Unicode-ordered base glyphs can be bracketed; inference stays explicit."""
    font = TTFont(FONT)
    font["post"].formatType = 3.0
    for cmap in font["cmap"].tables:
        if cmap.isUnicode():
            for cp in (0x0C25, 0x0C3F, 0x0C40):
                cmap.cmap.pop(cp, None)
    out = io.BytesIO()
    font.save(out)
    mapping = build_glyph_map(out.getvalue())
    text = "థి కీ"
    assert decode_glyph_stream(shape(text, FONT.read_bytes()), mapping).text == text
    assert any(r["source"] == "inferred Unicode-ordered cmap gap" for r in mapping.cmap_repairs)


def test_form_xobject_and_split_tj(tmp_path):
    """Resources belong to the Form, and syllables may span string operands."""
    import fitz

    source = make_pdf(tmp_path / "source.pdf", lines=[LINES[0]])
    with fitz.open(source) as original:
        content = original[0].read_contents()
        start = content.index(b"<")
        end = content.index(b">", start)
        hex_gids = content[start + 1 : end]
        # Split every glyph, including within conjuncts; numeric TJ spacing.
        operands = (
            b"["
            + b" 0 ".join(b"<" + hex_gids[i : i + 4] + b">" for i in range(0, len(hex_gids), 4))
            + b"]"
        )
        original.update_stream(
            original[0].get_contents()[0],
            content[:start] + operands + content[end + 1 :].replace(b"Tj", b"TJ"),
        )
        with fitz.open() as wrapper:
            page = wrapper.new_page(width=900, height=800)
            page.show_pdf_page(page.rect, original, 0)
            target = tmp_path / "form.pdf"
            wrapper.save(target)
    result = recover_pdf(target)
    assert result.text.strip() == LINES[0]
    assert result.report["roundtrip_match_ratio"] == 1


def test_unordered_cmap_not_inferred():
    font = TTFont(FONT)
    for cmap in font["cmap"].tables:
        if cmap.isUnicode():
            cmap.cmap = dict(cmap.cmap)
            cmap.cmap.pop(0x0C25, None)
            cmap.cmap[0x0C15], cmap.cmap[0x0C30] = cmap.cmap[0x0C30], cmap.cmap[0x0C15]
    font["post"].formatType = 3
    out = io.BytesIO()
    font.save(out)
    mapping = build_glyph_map(out.getvalue())
    assert not any(r["source"].startswith("inferred") for r in mapping.cmap_repairs)
    assert any(s["codepoint"] == "U+0C25" for s in mapping.skipped)


@pytest.mark.parametrize("text", ["స్త్ర్యై", "క్ష్త్ర్యౌ", "క్ట్ట్ర్యు", "స్త్ర్యీ"])
def test_rare_conjuncts_on_demand(text, glyph_map):
    gids = shape(text, FONT.read_bytes())
    result = decode_glyph_stream(gids, glyph_map)
    assert result.text == text
    assert result.unknown_count == 0
    assert verify_roundtrip(result.text, gids, glyph_map).match_ratio == 1


def test_notdef_is_not_roundtrip_evidence(glyph_map):
    result = verify_roundtrip("\ufffd", [0], glyph_map)
    assert result.match_ratio == 0
    assert result.mismatching_spans


def test_same_font_name_different_cmaps(tmp_path):
    """Resource xref, not the shared font name, must select the decoding map."""
    font = TTFont(FONT)
    for cmap in font["cmap"].tables:
        if cmap.isUnicode():
            cmap.cmap = dict(cmap.cmap)
            cmap.cmap[0x0C15], cmap.cmap[0x0C17] = cmap.cmap[0x0C17], cmap.cmap[0x0C15]
    out = io.BytesIO()
    font.save(out)
    pdf = make_pdf(
        tmp_path / "two-cmaps.pdf",
        lines=["క గ", "క గ"],
        multiple_fonts=True,
        second_font_data=out.getvalue(),
    )
    assert recover_pdf(pdf).text.strip() == "క గ\nక గ\nLatin 123"
