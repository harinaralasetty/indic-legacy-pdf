"""R8 independent source properties over two metric sets and five PDF encodings."""

import json
import time

import fitz
import pytest
import uharfbuzz as hb

from legacylipi.core import glyph_recovery as gr
from tests.glyph_ground_truth import (
    CORPUS,
    ENCODINGS,
    JOINERS,
    SERIF_FONT,
    geometry,
    ground_truth_pdf,
    larger_corpus,
)
from tests.glyph_helpers import FONT


@pytest.mark.parametrize("font_path", [FONT, SERIF_FONT], ids=["sans", "serif"])
def test_ground_truth_corpus(tmp_path, monkeypatch, font_path):
    start = time.perf_counter()
    assert len(CORPUS) >= 400
    data = font_path.read_bytes()
    original_steps = gr._shaped_steps
    stats = {"within": [], "between": []}
    failures = []
    for encoding in ENCODINGS:
        path = tmp_path / f"{encoding}.pdf"
        layouts = ground_truth_pdf(path, CORPUS, data, encoding)
        row = 0

        def observe(decoded, mapping, span, layouts=layouts, encoding=encoding):
            nonlocal row
            steps = original_steps(decoded, mapping, span)
            layout = layouts[row]
            assert span["gids"] == layout.gids, (encoding, CORPUS[row])
            for i, step in enumerate(steps):
                if i in layout.spaces or i + 1 in layout.spaces:
                    continue
                label = "between" if i + 1 in layout.boundaries else "within"
                residual = (span["origins"][i + 1] - span["origins"][i] - step) / span["sizes"][i]
                stats[label].append(residual)
            row += 1
            return steps

        monkeypatch.setattr(gr, "_shaped_steps", observe)
        result = gr.recover_pdf(path)
        actual = result.text.strip().splitlines()
        assert len(actual) == len(CORPUS)
        failures.extend(
            (encoding, source, recovered)
            for source, recovered in zip(CORPUS, actual, strict=True)
            if source != recovered
        )
        assert row == len(CORPUS)
    summary = {
        key: {"min": min(values), "max": max(values), "count": len(values)}
        for key, values in stats.items()
    }
    print(
        json.dumps(
            {
                "font": font_path.name,
                "cases": len(CORPUS),
                "encodings": len(ENCODINGS),
                "residual_em": summary,
                "seconds": time.perf_counter() - start,
            },
            ensure_ascii=False,
        )
    )
    assert not failures, failures[:30]
    assert summary["within"]["max"] < gr.WORD_GAP_EM - 0.05
    assert summary["between"]["min"] > gr.WORD_GAP_EM + 0.05


@pytest.mark.parametrize("font_path", [FONT, SERIF_FONT], ids=["sans", "serif"])
def test_encoding_geometry_and_pixels(tmp_path, font_path):
    texts = ["జ్ఞానం జ్ఞాపకం", "దీర్ఘం స్వాతంత్ర్యం", "ప్రభుత్వం క్షేత్రం విశాఖపట్నం", "రాష్ట్రం రాష్ట్రపతి"]
    data = font_path.read_bytes()
    reference = None
    for encoding in ENCODINGS:
        path = tmp_path / f"{encoding}.pdf"
        ground_truth_pdf(path, texts, data, encoding)
        with fitz.open(path) as doc:
            if encoding == "words":
                assert doc[0].read_contents().split().count(b"BT") == sum(
                    len(text.split()) for text in texts
                )
            pixels = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).samples
            trace = [
                (c[1], c[2]) for span in doc[0].get_texttrace() for c in span["chars"] if c[1] != 3
            ]
        if reference is None:
            reference = pixels, trace
        assert pixels == reference[0], encoding
        assert [g for g, _ in trace] == [g for g, _ in reference[1]]
        for (_, position), (_, expected) in zip(trace, reference[1], strict=True):
            assert position == pytest.approx(expected, abs=0.001)


@pytest.mark.parametrize("font_path", [FONT, SERIF_FONT], ids=["sans", "serif"])
def test_joiner_information_loss(tmp_path, font_path):
    data = font_path.read_bytes()
    font = hb.Font(hb.Face(data))
    # Distinct originals with identical glyphs AND geometry: exact inversion
    # is impossible without extra source metadata, independent of recovery.
    assert geometry(JOINERS[0], font) == geometry(JOINERS[1], font)
    for encoding in ENCODINGS:
        path = tmp_path / f"{encoding}.pdf"
        ground_truth_pdf(path, JOINERS, data, encoding)
        result = gr.recover_pdf(path)
        assert any(w["code"] == "ambiguous-joiner" for w in result.warnings)
        assert result.text.strip().splitlines() == ["క్క", "క్క"]


@pytest.mark.slow
@pytest.mark.parametrize("font_path", [FONT, SERIF_FONT], ids=["sans", "serif"])
def test_all_pair_classes(tmp_path, font_path):
    data = font_path.read_bytes()
    corpus = larger_corpus()
    path = tmp_path / "pairs.pdf"
    ground_truth_pdf(path, corpus, data, "Tm")
    actual = gr.recover_pdf(path).text.strip().splitlines()
    assert actual == corpus


@pytest.mark.parametrize("font_path", [FONT, SERIF_FONT], ids=["sans", "serif"])
def test_latin_fallback_mixed_phrases(tmp_path, font_path):
    """The Telugu fonts lack Latin: shape a real embedded fallback, never .notdef."""
    from pathlib import Path

    latin_data = (
        Path(__file__).parents[1] / "src/legacylipi/fonts/NotoSansDevanagari-Regular.ttf"
    ).read_bytes()
    latin_font = hb.Font(hb.Face(latin_data))
    data = font_path.read_bytes()
    telugu_font = hb.Font(hb.Face(data))
    for prefix, suffix in [("జ్ఞానం ", "PDF 2026"), ("దీర్ఘం ", "Unicode 123")]:
        buf = hb.Buffer()
        buf.add_str(prefix)
        buf.guess_segment_properties()
        hb.shape(telugu_font, buf)
        start_x = 40 + 14 * sum(p.x_advance for p in buf.glyph_positions) / telugu_font.face.upem
        layout = geometry(suffix, latin_font)
        assert 0 not in layout.gids
        for encoding in ENCODINGS:
            path = tmp_path / f"mixed-{encoding}.pdf"
            ground_truth_pdf(path, [prefix], data, encoding)
            parts = [f"BT /Fallback 14 Tf 1 0 0 1 {start_x:.8f} 55 Tm"]
            previous_x = 0.0
            for i, (gid, x) in enumerate(zip(layout.gids, layout.x, strict=True)):
                if encoding == "Td":
                    parts.append(f"{14 * (x - previous_x):.8f} 0 Td <{gid:04x}> Tj")
                elif encoding in ("Tm", "words"):
                    parts.append(f"1 0 0 1 {start_x + 14 * x:.8f} 55 Tm <{gid:04x}> Tj")
                else:
                    adjustment = 0.0
                    if i + 1 < len(layout.gids):
                        nominal = latin_font.get_glyph_h_advance(gid) / latin_font.face.upem
                        adjustment = (nominal - layout.x[i + 1] + x) * 1000
                    parts.append(f"[<{gid:04x}> {adjustment:.8f}] TJ")
                previous_x = x
            parts.append("ET")
            with fitz.open(path) as doc:
                doc[0].insert_font(fontname="Fallback", fontbuffer=latin_data)
                doc.update_stream(4, doc.xref_stream(4) + b"\n" + "\n".join(parts).encode())
                doc.saveIncr()
            fitz.TOOLS.mupdf_warnings(reset=True)
            assert gr.recover_pdf(path).text.strip() == prefix + suffix
            assert "syntax error" not in fitz.TOOLS.mupdf_warnings(reset=True)


def test_reading_order_between_disconnected_runs(tmp_path):
    data = FONT.read_bytes()
    path = tmp_path / "reading-order.pdf"
    ground_truth_pdf(path, ["జ్ఞానం"], data, "Tm")
    with fitz.open(path) as doc:
        first = doc.xref_stream(4)
        # Paint a disconnected right-hand copy first; order the two runs by x.
        doc.update_stream(4, b"q 1 0 0 1 200 0 cm " + first + b" Q " + first)
        doc.saveIncr()
    assert gr.recover_pdf(path).text.strip() == "జ్ఞానం\tజ్ఞానం"
