"""R1 regressions: independent synthetic ground truth, no private or /tmp inputs."""

import io
import os
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import fitz
import pytest
from click.testing import CliRunner
from fontTools.ttLib import TTFont

import legacylipi.core.glyph_recovery as gr
from legacylipi.cli import main
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.models import FontInfo, PDFDocument, PDFPage, TextBlock
from legacylipi.core.output_generator import OutputGenerator
from legacylipi.core.pdf_parser import parse_pdf
from legacylipi.core.translator import MockTranslationBackend, TranslationError
from legacylipi.core.unicode_converter import UnicodeConverter
from tests.glyph_helpers import FONT, make_pdf, shape


def change(path, content):
    with fitz.open(path) as doc:
        doc.update_stream(4, content)
        doc.saveIncr()


def operand(text):
    return (
        b"<"
        + b"".join(g.to_bytes(2, "big") for g in shape(text, FONT.read_bytes())).hex().encode()
        + b">"
    )


def test_r1_unused_legacy_matches_main(tmp_path):
    source = subprocess.check_output(
        ["git", "show", "main:src/legacylipi/core/encoding_detector.py"], text=True
    )
    old = types.ModuleType("review_main_detector")
    sys.modules[old.__name__] = old
    exec(compile(source, "main:encoding_detector.py", "exec"), old.__dict__)
    doc = PDFDocument(
        filepath=tmp_path / "absent.pdf",
        pages=[PDFPage(1, [TextBlock(raw_text="Hello world", font_name="NotoSansDevanagari")])],
        fonts=[FontInfo(name="NotoSansDevanagari"), FontInfo(name="KrutiDev010")],
    )
    outputs = []
    for detector in (old.EncodingDetector(), EncodingDetector()):
        _, pages = detector.detect_from_document(doc)
        outputs.append(UnicodeConverter().convert_document(doc, page_encodings=pages).unicode_text)
    assert outputs == ["Hello world", "Hello world"]


def test_r2_mixed_legacy_page(tmp_path):
    pdf = make_pdf(tmp_path / "mixed.pdf", lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        doc.new_page().insert_text((40, 60), "§¨²¡¢¥¦°þÿ»½¿ " * 3)
        doc.saveIncr()
    doc = parse_pdf(pdf)
    detector = EncodingDetector()
    overall, pages = detector.detect_from_document(doc)
    _, baseline = detector.detect_from_document(replace(doc, filepath=tmp_path / "absent"))
    converter = UnicodeConverter()
    assert pages[2] == baseline[2]
    assert (
        converter.convert_with_recovery(doc, overall, pages).pages[1].unicode_text
        == converter.convert_document(doc, page_encodings=baseline).pages[1].unicode_text
    )


@pytest.fixture
def hidden_swap(tmp_path):
    font = TTFont(FONT)
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap = dict(table.cmap)
            table.cmap[0xC25], table.cmap[0xC26] = table.cmap[0xC26], table.cmap[0xC25]
    font["post"].formatType = 3.0
    out = io.BytesIO()
    font.save(out)
    pdf = make_pdf(tmp_path / "hidden-swap.pdf", font_data=out.getvalue(), lines=["థ"])
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap.pop(0xC25, None)
            table.cmap.pop(0xC26, None)
    out = io.BytesIO()
    font.save(out)
    font.close()
    with fitz.open(pdf) as doc:
        doc.update_stream(8, out.getvalue())
        doc.update_stream(12, out.getvalue())
        doc.saveIncr()
    return pdf


def test_r3_hidden_swap_warning_and_strict(hidden_swap):
    result = gr.recover_pdf(hidden_swap)
    assert result.text.strip() == "ద"  # Deliberately wrong despite a perfect round trip.
    assert result.report["roundtrip_match_ratio"] == 1
    assert result.inferred_codepoints == ["U+0C26"]
    assert any(w["code"] == "inferred-cmap" for w in result.warnings)
    with pytest.raises(ValueError, match="inferred"):
        gr.recover_pdf(hidden_swap, strict_cmap=True)
    doc = parse_pdf(hidden_swap)
    overall, pages = EncodingDetector().detect_from_document(doc)
    converted = UnicodeConverter().convert_with_recovery(doc, overall, pages)
    assert converted.warnings == result.warnings
    assert converted.inferred_codepoints == result.inferred_codepoints


@pytest.mark.parametrize("command", ["recover-text", "translate", "convert", "extract"])
def test_r3_cli_warnings_and_strict(hidden_swap, tmp_path, command):
    args = [command, str(hidden_swap), "-o", str(tmp_path / "out.txt")]
    if command == "translate":
        args += ["--translator", "mock"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "inferred" in result.output.lower() and "warning" in result.output.lower()
    result = CliRunner().invoke(main, args + ["--strict-cmap"])
    assert result.exit_code != 0 and "inferred" in result.output.lower()


@pytest.mark.parametrize(
    "adjustment,expected",
    [(-1500, "తెలుగు తెలుగు"), (-20, "తెలుగుతెలుగు"), (20, "తెలుగుతెలుగు"), (1500, "తెలుగుతెలుగు")],
)
def test_r4_tj_spacing(tmp_path, adjustment, expected):
    pdf = make_pdf(tmp_path / "spacing.pdf", lines=["తెలుగుతెలుగు"])
    change(
        pdf,
        b"BT /F1 14 Tf 1 0 0 1 40 740 Tm ["
        + operand("తెలుగు")
        + str(adjustment).encode()
        + operand("తెలుగు")
        + b"] TJ ET",
    )
    assert gr.recover_pdf(pdf).text.strip() == expected


@pytest.mark.parametrize("mode", [2, 6])
def test_r5_fill_stroke(tmp_path, mode):
    pdf = make_pdf(tmp_path / "mode.pdf", lines=["తెలుగు"])
    change(
        pdf,
        b"BT /F1 14 Tf "
        + str(mode).encode()
        + b" Tr 1 0 0 1 40 740 Tm "
        + operand("తెలుగు")
        + b" Tj ET",
    )
    assert gr.recover_pdf(pdf).text.strip() == "తెలుగు"


@pytest.mark.parametrize("mode", [3, 7])
def test_r5_nonvisible_excluded(tmp_path, mode):
    pdf = make_pdf(tmp_path / "mode.pdf", lines=["తెలుగు"])
    change(
        pdf,
        b"BT /F1 14 Tf 1 0 0 1 40 740 Tm "
        + operand("తెలుగు")
        + b" Tj ET BT /F1 14 Tf "
        + str(mode).encode()
        + b" Tr 1 0 0 1 40 740 Tm "
        + operand("తెలుగు")
        + b" Tj ET",
    )
    assert gr.recover_pdf(pdf).text.strip() == "తెలుగు"


@pytest.mark.parametrize("joiner", ["\u200c", "\u200d"])
def test_r6_joiner_alias_is_not_space(joiner):
    result = gr.decode_glyph_stream(
        shape("క్" + joiner + "క", FONT.read_bytes()), gr.build_glyph_map(FONT.read_bytes())
    )
    assert result.text == "క్క"
    assert any(w["code"] == "ambiguous-joiner" for w in result.warnings)


def test_r7_output_gids_are_shaped(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "shape.pdf", lines=["స్త్రీ"])
    doc = parse_pdf(pdf)
    overall, pages = EncodingDetector().detect_from_document(doc)
    doc = UnicodeConverter().convert_with_recovery(doc, overall, pages)
    monkeypatch.setattr(OutputGenerator, "_get_unicode_font", lambda *a, **k: str(FONT))
    output = OutputGenerator(include_metadata=False).generate_pdf(doc, overall)
    with fitz.open(stream=output, filetype="pdf") as rendered:
        assert [c[1] for s in rendered[0].get_texttrace() for c in s["chars"]] == shape(
            "స్త్రీ", FONT.read_bytes()
        )
        assert rendered[0].get_text().strip() == "స్త్రీ"


@pytest.mark.parametrize("link", ["hard", "symbolic"])
@pytest.mark.parametrize("target", ["output", "report"])
def test_r8_path_alias_refused(tmp_path, link, target):
    pdf = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    before = pdf.read_bytes()
    alias = tmp_path / "alias.txt"
    (os.link if link == "hard" else os.symlink)(pdf, alias)
    args = [
        "recover-text",
        str(pdf),
        "-o",
        str(alias if target == "output" else tmp_path / "out.txt"),
    ]
    if target == "report":
        args += ["--report", str(alias)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert pdf.read_bytes() == before


def test_r8_existing_pdf_disguised_refused(tmp_path):
    pdf = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    output = make_pdf(tmp_path / "other.txt", lines=["తెలుగు"])
    before = output.read_bytes()
    result = CliRunner().invoke(main, ["recover-text", str(pdf), "-o", str(output)])
    assert result.exit_code != 0
    assert output.read_bytes() == before


def test_r8_atomic_outputs(tmp_path, monkeypatch):
    import legacylipi.cli as cli

    calls = []
    original = os.replace

    def tracked(src, dst):
        calls.append((str(src), str(dst)))
        return original(src, dst)

    monkeypatch.setattr(os, "replace", tracked)
    pdf = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    output = tmp_path / "out.txt"
    result = CliRunner().invoke(cli.main, ["recover-text", str(pdf), "-o", str(output)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert all(os.path.dirname(src) == os.path.dirname(dst) for src, dst in calls)


def test_r9_positioned_failure_nonzero(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise TranslationError("deliberate failure")

    monkeypatch.setattr(MockTranslationBackend, "translate", fail)
    pdf = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    result = CliRunner().invoke(
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
            str(tmp_path / "out.pdf"),
        ],
    )
    assert result.exit_code != 0
    assert "failed" in result.output.lower()


def fanout(path, depth):
    pdf = make_pdf(path, lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        child = doc.get_new_xref()
        doc.update_object(
            child,
            "<< /Type /XObject /Subtype /Form /BBox [0 0 900 800] /Resources << /Font << /F1 5 0 R >> >> >>",
        )
        doc.update_stream(child, doc[0].read_contents())
        for _ in range(depth):
            parent = doc.get_new_xref()
            doc.update_object(
                parent,
                f"<< /Type /XObject /Subtype /Form /BBox [0 0 900 800] /Resources << /XObject << /X {child} 0 R >> >> >>",
            )
            doc.update_stream(parent, b"/X Do /X Do")
            child = parent
        doc.xref_set_key(3, "Resources", f"<< /XObject << /X {child} 0 R >> >>")
        doc.update_stream(4, b"/X Do")
        doc.saveIncr()
    return pdf


@pytest.mark.parametrize("depth", [8, 12, 16])
def test_r10_form_discovery_linear(tmp_path, depth):
    pdf = fanout(tmp_path / "fanout.pdf", depth)
    start = time.perf_counter()
    assert gr.find_glyph_id_fonts(pdf)
    assert time.perf_counter() - start < 1


def test_r10_roundtrip_identical_fast():
    mapping = gr.build_glyph_map(FONT.read_bytes())
    text = "క" * 6000
    gids = shape(text, FONT.read_bytes())
    start = time.perf_counter()
    assert gr.verify_roundtrip(text, gids, mapping).match_ratio == 1
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_OPERATIONS",
        "MAX_FORM_EXPANSIONS",
        "MAX_STREAM_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_ALIGNMENT_WORK",
    ],
)
def test_r10_budget_errors(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(gr, limit, 1)
    with pytest.raises(ValueError, match="budget"):
        if limit == "MAX_ALIGNMENT_WORK":
            gr.verify_roundtrip("తెలుగు", [65535] * 100, gr.build_glyph_map(FONT.read_bytes()))
        else:
            gr.recover_pdf(fanout(tmp_path / "budget.pdf", 3))


def test_r11_concurrent_cache_coalesces():
    gr._CACHE.clear()
    barrier = Barrier(2)

    def build():
        barrier.wait()
        return gr.build_glyph_map(FONT.read_bytes())

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(build), pool.submit(build)
        assert a.result() is b.result()


@pytest.mark.parametrize(
    "content",
    [b"BT Tf ET", b"BT /F1 Tf ET", b"BT Tr ET", b"BT Tj ET", b"BT TJ ET", b"BT Tm ET", b"Do"],
)
def test_r12_malformed_controlled(tmp_path, content):
    pdf = make_pdf(tmp_path / "bad.pdf", lines=["తెలుగు"])
    change(pdf, content)
    assert gr.find_glyph_id_fonts(pdf) == []
    with pytest.raises(ValueError):
        gr.recover_pdf(pdf)


def test_r13_candidate_font_budget(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "many.pdf", lines=["తెలుగు", "తెలుగు"], multiple_fonts=True)
    monkeypatch.setattr(gr, "MAX_CANDIDATE_FONTS", 1)
    with pytest.raises(ValueError, match="budget"):
        gr.recover_pdf(pdf)


def test_r13_map_memory_budget(monkeypatch):
    gr._CACHE.clear()
    monkeypatch.setattr(gr, "MAX_MAP_BYTES", 1024)
    with pytest.raises(ValueError, match="budget"):
        gr.build_glyph_map(FONT.read_bytes())


@pytest.mark.asyncio
async def test_r3_api_session_exposes_warning(hidden_swap):
    from legacylipi.api.pipeline import run_translate
    from legacylipi.api.routes.processing import _run_pipeline
    from legacylipi.api.schemas import TranslateRequest
    from legacylipi.api.session_manager import SessionManager

    manager = SessionManager()
    session = await manager.create_session(hidden_swap.name, hidden_swap.read_bytes())
    await _run_pipeline(
        manager,
        session.session_id,
        run_translate(
            session.file_bytes,
            session.filename,
            TranslateRequest(translator="mock", output_format="text"),
        ),
    )
    assert session.warnings[0]["code"] == "inferred-cmap"
    assert session.inferred_codepoints == ["U+0C26"]
    event = await session.progress_queue.get()
    assert event.warnings == session.warnings
    assert event.inferred_codepoints == session.inferred_codepoints
    with pytest.raises(ValueError, match="inferred"):
        await run_translate(
            session.file_bytes,
            session.filename,
            TranslateRequest(translator="mock", output_format="text", strict_cmap=True),
        )


@pytest.mark.parametrize("zero_advance", [True, False])
def test_r6_pdf_joiner_positions(tmp_path, zero_advance):
    pdf = make_pdf(tmp_path / "joiner.pdf", lines=["క్\u200cక" if zero_advance else "క్ క"])
    if zero_advance:
        with fitz.open(pdf) as doc:
            doc.xref_set_key(6, "W", "[3 [0]]")
            doc.saveIncr()
    result = gr.recover_pdf(pdf)
    assert result.text.strip() == ("క్క" if zero_advance else "క్ క")
    assert bool(result.warnings) == zero_advance


def test_r13_many_distinct_fonts_default_budget(tmp_path):
    pdf = make_pdf(tmp_path / "many.pdf", lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        resources = ["/F1 5 0 R"]
        content = [doc[0].read_contents()]
        for i in range(gr.MAX_CANDIDATE_FONTS):
            font = TTFont(FONT)
            font["name"].setName(f"Synthetic budget font {i}", 1, 3, 1, 0x409)
            out = io.BytesIO()
            font.save(out)
            font.close()
            root = doc.get_new_xref()
            child = doc.get_new_xref()
            descriptor = doc.get_new_xref()
            program = doc.get_new_xref()
            doc.update_object(
                root,
                f"<< /Type /Font /Subtype /Type0 /BaseFont /Budget{i} /Encoding /Identity-H /DescendantFonts [{child} 0 R] >>",
            )
            doc.update_object(
                child,
                f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Budget{i} /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 600 /CIDToGIDMap /Identity /FontDescriptor {descriptor} 0 R >>",
            )
            doc.update_object(
                descriptor,
                f"<< /Type /FontDescriptor /FontName /Budget{i} /Flags 4 /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 1000 /Descent -500 /CapHeight 700 /StemV 80 /FontFile2 {program} 0 R >>",
            )
            doc.update_object(program, "<< >>")
            doc.update_stream(program, out.getvalue())
            resources.append(f"/B{i} {root} 0 R")
            content.append(f"BT /B{i} 14 Tf ".encode() + operand("తెలుగు") + b" Tj ET")
        doc.xref_set_key(3, "Resources", "<< /Font << " + " ".join(resources) + " >> >>")
        doc.update_stream(4, b"\n".join(content))
        doc.saveIncr()
    with pytest.raises(ValueError, match="Candidate font budget"):
        gr.recover_pdf(pdf)


def test_r10_compressed_stream_budget(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "compressed.pdf", lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        doc.update_stream(4, b" " * 100_000 + doc[0].read_contents(), compress=True)
        doc.saveIncr()
    monkeypatch.setattr(gr, "MAX_STREAM_BYTES", 80_000)
    with pytest.raises(ValueError, match="budget"):
        gr.find_glyph_id_fonts(pdf)


@pytest.mark.asyncio
async def test_r9_partial_failure_structured():
    from legacylipi.core.translator import create_translator

    engine = create_translator("mock")

    async def selective(text, source, target):
        if text == "fail":
            raise TranslationError("one failure")
        return "translated"

    engine._backend.translate = selective
    blocks = [TextBlock(raw_text=text) for text in ("fail", "ok", "ok")]
    result = await engine.translate_blocks_async(blocks)
    assert result[0].translation_error == "one failure"
    assert result[0].translated_text == "fail"
    assert result[1].translated_text == "translated"


@pytest.mark.parametrize("adjustment", [-20, 20])
def test_r4_tj_kerning_inside_syllable(tmp_path, adjustment):
    pdf = make_pdf(tmp_path / "kerning.pdf", lines=["స్త్రీ"])
    gids = shape("స్త్రీ", FONT.read_bytes())
    operands = ("[" + f" {adjustment} ".join(f"<{gid:04x}>" for gid in gids) + "] TJ").encode()
    change(pdf, b"BT /F1 14 Tf 1 0 0 1 40 740 Tm " + operands + b" ET")
    assert gr.recover_pdf(pdf).text.strip() == "స్త్రీ"


def test_r1_unicode_block_on_legacy_page(tmp_path):
    doc = PDFDocument(
        filepath=tmp_path / "absent",
        pages=[
            PDFPage(
                1,
                [
                    TextBlock(raw_text="Hello world", font_name="NotoSansDevanagari"),
                    TextBlock(raw_text="hello", font_name="KrutiDev010"),
                ],
            )
        ],
    )
    overall, pages = EncodingDetector().detect_from_document(doc)
    converted = UnicodeConverter().convert_with_recovery(doc, overall, pages)
    assert converted.pages[0].text_blocks[0].unicode_text == "Hello world"
    assert converted.pages[0].text_blocks[1].unicode_text != "hello"


def test_r10_aggregate_alignment_budget(monkeypatch):
    mapping = gr.build_glyph_map(FONT.read_bytes())
    gids = shape("తలుగు", FONT.read_bytes())
    cost = len(shape("తెలుగు", FONT.read_bytes())) * len(gids)
    monkeypatch.setattr(gr, "MAX_ALIGNMENT_WORK", cost)
    budget = gr._Budget()
    gr.verify_roundtrip("తెలుగు", gids, mapping, _budget=budget)
    with pytest.raises(ValueError, match="budget"):
        gr.verify_roundtrip("తెలుగు", gids, mapping, _budget=budget)
