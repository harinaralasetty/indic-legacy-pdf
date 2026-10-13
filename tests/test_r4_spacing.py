"""Equivalent positioning operators must recover identical word boundaries."""

from itertools import count
from types import SimpleNamespace

import fitz
import pytest

from legacylipi.core.glyph_recovery import recover_pdf
from tests.glyph_helpers import FONT, make_pdf, shape
from tests.test_glyph_review import change, operand


@pytest.mark.parametrize("gap", [0.02, 0.149, 0.151, 0.2, 0.3, 1.5])
def test_r4_equivalent_tj_tm_td_word_boundaries(tmp_path, gap):
    traces, texts = [], []
    first, second = operand("తెలుగు").decode(), operand("భాష").decode()
    step = len(shape("తెలుగు", FONT.read_bytes())) * 14 * 0.6 + gap * 14
    for mode in ("TJ", "Tm", "Td"):
        pdf = make_pdf(tmp_path / f"{mode}.pdf", lines=[])
        if mode == "TJ":
            ops = f"[{first} {-gap * 1000} {second}] TJ"
        else:
            move = f"1 0 0 1 {40 + step} 740 Tm" if mode == "Tm" else f"{step} 0 Td"
            ops = f"{first} Tj {move} {second} Tj"
        change(pdf, f"BT /F1 14 Tf 1 0 0 1 40 740 Tm {ops} ET".encode())
        with fitz.open(pdf) as doc:
            traces.append([(c[1], c[2][0]) for s in doc[0].get_texttrace() for c in s["chars"]])
        texts.append(recover_pdf(pdf).text.strip())
    for trace in traces[1:]:
        assert [g for g, _ in trace] == [g for g, _ in traces[0]]
        assert [x for _, x in trace] == pytest.approx([x for _, x in traces[0]], abs=0.001)
    assert texts == ["తెలుగు భాష" if gap > 0.15 else "తెలుగుభాష"] * 3


@pytest.mark.parametrize("text", ["స్త్రీ", "జ్ఞా", "క్ష్మ్య"])
@pytest.mark.parametrize("gap", [0.02, 0.2, 0.3])
def test_r4_equivalent_positioning_preserves_clusters(tmp_path, text, gap):
    gids = shape(text, FONT.read_bytes())
    outputs = []
    for mode in ("TJ", "Tm"):
        pdf = make_pdf(tmp_path / f"cluster-{mode}.pdf", lines=[])
        if mode == "TJ":
            ops = "[" + f" {-gap * 1000} ".join(f"<{g:04x}>" for g in gids) + "] TJ"
        else:
            ops = " ".join(
                f"1 0 0 1 {40 + i * (0.6 + gap) * 14} 740 Tm <{g:04x}> Tj"
                for i, g in enumerate(gids)
            )
        change(pdf, f"BT /F1 14 Tf 1 0 0 1 40 740 Tm {ops} ET".encode())
        outputs.append(recover_pdf(pdf).text.strip())
    assert outputs == [text, text]


def test_r4_timing_checks_tolerate_scheduler_pauses(tmp_path, monkeypatch):
    from tests import test_glyph_review as review

    ticks = count(0, 100)
    monkeypatch.setattr(
        review, "time", SimpleNamespace(perf_counter=lambda: next(ticks)), raising=False
    )
    review.test_r10_form_discovery_linear(tmp_path, 12)
    review.test_r10_roundtrip_identical_fast()


@pytest.mark.parametrize("text", ["ఝ్కృ", "జ్ఞా", "క్ష్మ్య"])
def test_r4_shaped_kerning_operator_equivalence(tmp_path, text):
    import uharfbuzz as hb
    from fontTools.ttLib import TTFont

    font = hb.Font(hb.Face(FONT.read_bytes()))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    infos, positions = buf.glyph_infos, buf.glyph_positions
    assert all(p.y_offset == 0 for p in positions)
    with TTFont(FONT) as tt:
        order = tt.getGlyphOrder()
        widths = [tt["hmtx"][order[g.codepoint]][0] for g in infos]
    results = []
    for mode in ("TJ", "Tm"):
        pdf = make_pdf(tmp_path / f"shaped-{mode}.pdf", lines=[])
        operands = []
        x = 40
        for i, (glyph, position) in enumerate(zip(infos, positions, strict=True)):
            if mode == "Tm":
                operands.append(
                    f"1 0 0 1 {x + position.x_offset * 14 / font.face.upem} 740 Tm <{glyph.codepoint:04x}> Tj"
                )
                x += position.x_advance * 14 / font.face.upem
            else:
                operands.append(f"<{glyph.codepoint:04x}>")
                if i + 1 < len(infos):
                    operands.append(
                        str(
                            (
                                widths[i]
                                - position.x_advance
                                + position.x_offset
                                - positions[i + 1].x_offset
                            )
                            * 1000
                            / font.face.upem
                        )
                    )
        ops = " ".join(operands)
        if mode == "TJ":
            ops = "[" + ops + "] TJ"
        with fitz.open(pdf) as doc:
            doc.xref_set_key(
                6,
                "W",
                "["
                + " ".join(
                    f"{g.codepoint} [{w * 1000 / font.face.upem}]"
                    for g, w in zip(infos, widths, strict=True)
                )
                + "]",
            )
            doc.update_stream(4, f"BT /F1 14 Tf 1 0 0 1 40 740 Tm {ops} ET".encode())
            doc.saveIncr()
        results.append(recover_pdf(pdf).text.strip())
    assert results == [text, text]
