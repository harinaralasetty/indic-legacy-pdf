"""Self-contained reproductions of R5 discovery, spacing and CID failures."""

import fitz
import pytest
from click.testing import CliRunner

import legacylipi.core.glyph_recovery as gr
from legacylipi.cli import main
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.pdf_parser import parse_pdf
from tests.glyph_helpers import make_pdf


def test_unused_identity_font_budget_is_inconclusive(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "ordinary.pdf", lines=[])
    with fitz.open(pdf) as doc:
        doc.update_stream(
            4, b"q Q\n" * 1_000_001 + b"BT /F3 12 Tf 40 700 Td (Hello ordinary Latin) Tj ET"
        )
        doc.saveIncr()
    document = parse_pdf(pdf)
    assert "Hello ordinary Latin" in document.raw_text
    detector = EncodingDetector()
    assert detector.detect_from_document(document) == detector._detect_ordinary_document(document)
    assert "inconclusive" in caplog.text.lower()
    with pytest.raises(gr.RecoveryBudgetError, match="operation budget"):
        gr.recover_pdf(pdf)
    result = CliRunner().invoke(main, ["recover-text", str(pdf), "-o", str(tmp_path / "out.txt")])
    assert result.exit_code != 0 and "operation budget" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "key,value",
    [
        ("W", "[1]"),
        ("W", "[1 2]"),
        ("W", "[1 2 600 4]"),
        ("W", "[1 [600 /bad]]"),
        ("W", "[1 /bad 600]"),
        ("W", "[3 1 600]"),
        ("W", "[1 2 /bad]"),
        ("W", "[1.5 [600]]"),
        ("W", "/bad"),
        ("W", "600"),
        ("DW", "/bad"),
        ("DW", "[600]"),
        ("CIDToGIDMap", "/bad"),
        ("DescendantFonts", "[6 0 R 10 0 R]"),
    ],
)
def test_malformed_cid_dictionary_is_controlled(tmp_path, key, value):
    pdf = make_pdf(tmp_path / "malformed.pdf", lines=["తెలుగు"])
    with fitz.open(pdf) as doc:
        doc.xref_set_key(5 if key == "DescendantFonts" else 6, key, value)
        doc.saveIncr()
    with pytest.raises(ValueError, match=key):
        gr.recover_pdf(pdf)
    result = CliRunner().invoke(main, ["recover-text", str(pdf), "-o", str(tmp_path / "out.txt")])
    assert result.exit_code != 0
    assert key in result.output and "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def spaced_pdf(
    path,
    *,
    text="తెలుగు",
    tc=0,
    tw=0,
    tz=100,
    size=14,
    sx=1,
    sy=1,
    rise=0,
    leading=0,
    form="TJ",
    gap=0,
    justify=0,
):
    """R5 heading probe, using real font widths and HB positions as independent input."""
    import uharfbuzz as hb
    from fontTools.ttLib import TTFont

    from tests.glyph_helpers import FONT

    font = hb.Font(hb.Face(FONT.read_bytes()))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    with TTFont(FONT) as tt:
        order = tt.getGlyphOrder()
        widths = [
            tt["hmtx"][order[g.codepoint]][0] * 1000 / font.face.upem for g in buf.glyph_infos
        ]
    parts = [
        f"BT /F1 {size} Tf {tc} Tc {tw} Tw {tz} Tz {rise} Ts {leading} TL {sx} 0 0 {sy} 40 740 Tm"
    ]
    x = 0.0
    step = 0.0
    ops = []
    for i, (glyph, pos) in enumerate(zip(buf.glyph_infos, buf.glyph_positions, strict=True)):
        operand = f"<{glyph.codepoint:04x}>"
        if form == "TJ":
            ops.append(operand)
        else:
            if form == "Tm":
                parts.append(f"{sx} 0 0 {sy} {40 + x * sx} 740 Tm")
            elif i:
                parts.append(f"{step} 0 Td")
            parts.append(operand + " Tj")
        if i + 1 < len(widths):
            advance = (
                (pos.x_advance - pos.x_offset + buf.glyph_positions[i + 1].x_offset)
                * 1000
                / font.face.upem
            )
            # A layout-only gap between two words without an encoded space.
            extra = gap if i + 1 == len(widths) // 2 and text == "తెలుగుతెలుగు" else 0
            # Justification after encoded spaces, in addition to any tracking.
            if text[buf.glyph_infos[i].cluster : buf.glyph_infos[i].cluster + 1] == " ":
                extra += justify
            ops.append(str(widths[i] - advance - extra * 1000))
            step = (advance / 1000 * size + tc + extra * size) * tz / 100
            x += step
    if form == "TJ":
        parts.append("[" + " ".join(ops) + "] TJ")
    parts.append("ET")
    pdf = make_pdf(path, lines=[])
    with fitz.open(pdf) as doc:
        doc.xref_set_key(
            6,
            "W",
            "["
            + " ".join(f"{g.codepoint} [{w}]" for g, w in zip(buf.glyph_infos, widths, strict=True))
            + "]",
        )
        doc.update_stream(4, " ".join(parts).encode())
        doc.saveIncr()
    return pdf


@pytest.mark.parametrize("form", ["TJ", "Td", "Tm"])
@pytest.mark.parametrize(
    "options",
    [
        {"tc": 3},
        {"tc": 3, "tw": 12},
        {"tc": 3, "tz": 50},
        {"tc": 3, "tz": 180},
        {"tc": 3, "sx": 2, "sy": 0.7},
        {"tc": 3, "size": 7},
        {"tc": 3, "size": 40},
        {"tc": 3, "rise": 8, "leading": 22},
        {"tc": -1},
    ],
)
def test_tracking_is_not_a_word_gap(tmp_path, form, options):
    result = gr.recover_pdf(spaced_pdf(tmp_path / "tracking.pdf", form=form, **options))
    assert result.text.strip() == "తెలుగు"
    assert not any(w["code"] == "spacing_ambiguity" for w in result.warnings)


@pytest.mark.parametrize("form", ["TJ", "Td", "Tm"])
@pytest.mark.parametrize("tc,tz,sx", [(0, 100, 1), (3, 100, 1), (3, 50, 2), (3, 180, 0.7)])
def test_real_word_gap_with_tracking(tmp_path, form, tc, tz, sx):
    result = gr.recover_pdf(
        spaced_pdf(
            tmp_path / "words.pdf", text="తెలుగుతెలుగు", gap=0.5, form=form, tc=tc, tz=tz, sx=sx
        )
    )
    assert result.text.strip() == "తెలుగు తెలుగు"


@pytest.mark.parametrize("gap,expected", [(0.12, "తెలుగుతెలుగు"), (0.2, "తెలుగు తెలుగు")])
def test_uncertain_spacing_is_disclosed(tmp_path, gap, expected):
    result = gr.recover_pdf(
        spaced_pdf(tmp_path / "uncertain.pdf", text="తెలుగుతెలుగు", tc=3, gap=gap)
    )
    assert result.text.strip() == expected
    warnings = [w for w in result.warnings if w["code"] == "spacing_ambiguity"]
    assert warnings and warnings[0]["page"] == 1
    assert "spacing_ambiguity" in result.report
    assert result.report["spacing_ambiguity"] == warnings


@pytest.mark.parametrize("tc,tw,tz", [(0, 0, 100), (3, 20, 100), (3, 20, 50)])
def test_justified_encoded_spaces(tmp_path, tc, tw, tz):
    result = gr.recover_pdf(
        spaced_pdf(
            tmp_path / "justified.pdf", text="తెలుగు తెలుగు తెలుగు", tc=tc, tw=tw, tz=tz, justify=0.6
        )
    )
    assert result.text.strip() == "తెలుగు తెలుగు తెలుగు"


def test_graphics_restore_does_not_restore_text_matrix(tmp_path):
    pdf = spaced_pdf(tmp_path / "matrix.pdf", tc=3, sx=2)
    with fitz.open(pdf) as doc:
        content = doc.xref_stream(4).replace(b"2 0 0 1 40 740 Tm", b"q 2 0 0 1 40 740 Tm Q")
        doc.update_stream(4, content)
        doc.saveIncr()
    assert gr.recover_pdf(pdf).text.strip() == "తెలుగు"


@pytest.mark.parametrize("command", ["extract", "translate", "convert"])
@pytest.mark.parametrize("failure", ["budget", "widths"])
def test_forced_recovery_cli_errors_are_clean(tmp_path, monkeypatch, command, failure):
    pdf = make_pdf(tmp_path / "forced.pdf", lines=["తెలుగు"])
    if failure == "budget":
        monkeypatch.setattr(gr, "MAX_OPERATIONS", 2)
        message = "operation budget"
    else:
        with fitz.open(pdf) as doc:
            doc.xref_set_key(6, "W", "[1]")
            doc.saveIncr()
        message = "Invalid CID W"
    args = [command, str(pdf), "--encoding", "glyph-id", "-o", str(tmp_path / "out.txt")]
    if command == "translate":
        args += ["--translator", "mock"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0 and message in result.output
    assert "Traceback" not in result.output
