"""PDFs authored from Unicode + HarfBuzz, never from recovery output.

Both Noto fonts are OFL fixtures. Serif v2.005 was copied from the local
telugu-m0/proto/references collection; see the adjacent OFL license.
"""

import io
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import fitz
import uharfbuzz as hb
from fontTools.ttLib import TTFont

from tests.glyph_helpers import FONT, make_pdf

SERIF_FONT = FONT.with_name("NotoSerifTelugu-Regular.ttf")
ENCODINGS = ("TJ", "Td", "Tm", "words", "gaps")
# Independent source vocabulary: no round-trip filtering or canonicalization.
WORDS = """
జ్ఞానం జ్ఞాపకం దీర్ఘం స్వాతంత్ర్యం ప్రభుత్వం క్షేత్రం విశాఖపట్నం
తెలుగు భాష అక్షరం అక్షరాలు పుస్తకం పాఠశాల విద్య విద్యార్థి విద్యార్థులు
ఉపాధ్యాయుడు ఉపాధ్యాయులు విశ్వవిద్యాలయం గ్రంథాలయం పరిశోధన విజ్ఞానం
సాంకేతిక విజ్ఞానశాస్త్రం గణితం సంఖ్య అంకె లెక్క పరీక్ష ప్రశ్న సమాధానం
చరిత్ర భూగోళం ప్రకృతి పర్యావరణం ఆరోగ్యం వైద్యం వైద్యుడు ఆసుపత్రి
వ్యవసాయం రైతు పంట పొలం వరి బియ్యం అన్నం నీరు భూమి గాలి నిప్పు ఆకాశం
సూర్యుడు చంద్రుడు నక్షత్రం మేఘం వర్షం వృక్షం చెట్టు మొక్క పువ్వు పండు
అడవి కొండ పర్వతం నది సముద్రం సరస్సు గ్రామం పట్టణం నగరం రాష్ట్రం దేశం
ప్రపంచం ప్రజలు ప్రజాస్వామ్యం రాజ్యాంగం హక్కు న్యాయం న్యాయస్థానం చట్టం
సభ సభ్యుడు అధ్యక్షుడు మంత్రి ప్రధానమంత్రి ఎన్నిక ఓటు అధికారం బాధ్యత
సేవ సంఘం సంస్థ సమాజం కుటుంబం తల్లి తండ్రి అమ్మ నాన్న అన్న అక్క
చెల్లి తమ్ముడు బిడ్డ బాలుడు బాలిక స్నేహం స్నేహితుడు ప్రేమ ఆనందం సంతోషం
దుఃఖం శాంతి ధైర్యం శక్తి భక్తి బుద్ధి శబ్దం అర్థం ధర్మం కర్మ లక్ష్యం
లక్ష్మి లక్ష్మీ శ్రీ స్త్రీ శ్రీవిద్య కృష్ణా ప్రక్రియ సంప్రదాయం సంస్కృతి
సంగీతం సాహిత్యం కవిత కథ రచన రచయిత నాటకం నృత్యం చిత్రం శిల్పం
పండుగ ఉత్సవం సంక్రాంతి ఉగాది దీపావళి వినాయకుడు దేవాలయం ప్రార్థన
ఉదయం మధ్యాహ్నం సాయంత్రం రాత్రి రోజు వారం నెల సంవత్సరం కాలం సమయం
గంట నిమిషం క్షణం నేడు రేపు నిన్న ఎల్లుండి ముందు వెనుక పైన కింద
తూర్పు పడమర ఉత్తరం దక్షిణం కేంద్రం దూరం దగ్గర ప్రయాణం రైలు బస్సు
వాహనం రహదారి మార్గం వంతెన నౌక విమానం స్టేషన్ యంత్రం పరిశ్రమ
ఉత్పత్తి వ్యాపారం వస్తువు ధర ధనం ఆర్థిక అభివృద్ధి ప్రగతి ప్రణాళిక
నిర్మాణం ప్రయత్నం కృషి విజయం అపజయం అవకాశం అవసరం ఉపయోగం సహాయం
వివరం సమాచారం సందేశం వార్త పత్రిక ప్రసారం కార్యక్రమం సమావేశం నిర్ణయం
సమస్య పరిష్కారం కారణం ఫలితం విధానం విధి నియమం క్రమం ప్రారంభం ముగింపు
అర్ఘ్యం నిర్ఘాతం ఊర్ధ్వం చిహ్నం బ్రహ్మ బ్రాహ్మణ ప్రహ్లాద యజ్ఞం అజ్ఞానం
అభ్యర్థన కర్తృత్వం ధ్వంసం వాక్చాతుర్యం సంక్షిప్తం లక్ష్మ్యా స్పష్టం
స్మృతి స్వేచ్ఛ స్వరం స్థలం స్థితి స్థిరం దృష్టి సృష్టి వృద్ధి తృప్తి
త్రయం ద్వారం ద్వితీయం పత్రం మిత్రం శత్రువు పాత్ర సూత్రం శాస్త్రం శాస్త్రవేత్త
వస్త్రం రాష్ట్రపతి క్షమ క్షేమం క్షీరం క్షణికం క్షీణత జ్ఞాని అజ్ఞాత
ఋషి ఋతువు కృప కృతజ్ఞత కౌశలం కైక లక్షణం పౌరుడు మౌనం సౌందర్యం
""".split()
# Every Telugu consonant in repeated, ra/ya/va conjunct families, with matras
# and both anusvara and visarga. These are orthographic probes, not word claims.
CONSONANTS = "కఖగఘఙచఛజఝఞటఠడఢణతథదధనపఫబభమయరఱలళవశషసహ"
ORTHOGRAPHIC = [c + "్" + tail for c in CONSONANTS for tail in (c + "ా", "రి", "యుం", "వః")]
PHRASES = [f"{word} {WORDS[(i * 17 + 9) % len(WORDS)]}" for i, word in enumerate(WORDS)]
MIXED = ["తెలుగు 2026", "తెలుగు ౧౨౩", "జ్ఞానం జ్ఞాపకం", "దీర్ఘం క్షేత్రం"]
CORPUS = tuple(dict.fromkeys(WORDS + PHRASES + ORTHOGRAPHIC + MIXED))
JOINERS = ("క్\u200cక", "క్\u200dక")


@dataclass
class Geometry:
    gids: list[int]
    x: list[float]
    y: list[float]
    boundaries: set[int]
    spaces: set[int]
    word_ids: list[int]


def geometry(text: str, font: hb.Font, omit_spaces: bool = False) -> Geometry:
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer)
    gids, xs, ys, words = [], [], [], []
    boundaries, spaces = set(), set()
    pen_x = pen_y = 0
    pending_space = False
    word = 0
    for glyph, pos in zip(buffer.glyph_infos, buffer.glyph_positions, strict=True):
        space = text[glyph.cluster] == " "
        if not (omit_spaces and space):
            if pending_space:
                boundaries.add(len(gids))
                pending_space = False
            if space:
                spaces.add(len(gids))
            gids.append(glyph.codepoint)
            xs.append((pen_x + pos.x_offset) / font.face.upem)
            ys.append((pen_y + pos.y_offset) / font.face.upem)
            words.append(word)
        if space:
            pending_space = True
            word += 1
        pen_x += pos.x_advance
        pen_y += pos.y_advance
    return Geometry(gids, xs, ys, boundaries, spaces, words)


def ground_truth_pdf(path: Path, texts, data: bytes, encoding: str):
    """Five equivalent representations of independently shaped source geometry."""
    font = hb.Font(hb.Face(data))
    with TTFont(io.BytesIO(data)) as tt:
        widths = [tt["hmtx"][name][0] / font.face.upem for name in tt.getGlyphOrder()]
    height = 60 + 30 * len(texts)
    parts = []
    layouts = []
    for row, text in enumerate(texts):
        layout = geometry(text, font, omit_spaces=encoding == "gaps")
        assert 0 not in layout.gids, f"Font cannot represent source: {text!r}"
        layouts.append(layout)
        baseline = height - 35 - row * 30
        parts.append(f"BT /F1 14 Tf 1 0 0 1 40 {baseline} Tm")
        if encoding in ("TJ", "gaps", "words"):
            # TJ can move only horizontally. Split at vertical GPOS changes
            # and use text rise to preserve the actual shaped geometry.
            groups = []
            for i in range(len(layout.gids)):
                if (
                    not groups
                    or layout.y[i] != layout.y[i - 1]
                    or (encoding == "words" and layout.word_ids[i] != layout.word_ids[i - 1])
                ):
                    groups.append([])
                groups[-1].append(i)
            for group_index, group in enumerate(groups):
                if (
                    group_index
                    and encoding == "words"
                    and layout.word_ids[group[0]] != layout.word_ids[group[0] - 1]
                ):
                    parts.append(
                        f"ET BT /F1 14 Tf 1 0 0 1 {40 + 14 * layout.x[group[0]]:.8f} {baseline} Tm"
                    )
                parts.append(f"{14 * layout.y[group[0]]:.8f} Ts")
                ops = [f"{-layout.x[0] * 1000:.8f}"] if not group_index else []
                for i in group:
                    ops.append(f"<{layout.gids[i]:04x}>")
                    if i + 1 < len(layout.gids) and not (
                        encoding == "words" and layout.word_ids[i + 1] != layout.word_ids[i]
                    ):
                        ops.append(
                            f"{(widths[layout.gids[i]] - layout.x[i + 1] + layout.x[i]) * 1000:.8f}"
                        )
                parts.append("[" + " ".join(ops) + "] TJ")
        else:
            previous_x = previous_y = 0.0
            for gid, x, y in zip(layout.gids, layout.x, layout.y, strict=True):
                if encoding == "Tm":
                    parts.append(f"1 0 0 1 {40 + 14 * x:.8f} {baseline + 14 * y:.8f} Tm")
                else:
                    parts.append(f"{14 * (x - previous_x):.8f} {14 * (y - previous_y):.8f} Td")
                parts.append(f"<{gid:04x}> Tj")
                previous_x, previous_y = x, y
        parts.append("ET")
    make_pdf(path, lines=[], font_data=data)
    with fitz.open(path) as doc:
        doc.xref_set_key(3, "MediaBox", f"[0 0 1600 {height}]")
        doc.xref_set_key(6, "W", "[0 [" + " ".join(str(w * 1000) for w in widths) + "]]")
        doc.update_stream(4, "\n".join(parts).encode())
        doc.saveIncr()
    return layouts


def larger_corpus():
    """Exhaustive two-consonant classes, kept out of the default property corpus."""
    return [a + "్" + b + sign for a, b, sign in product(CONSONANTS, CONSONANTS, ("", "ా", "ి"))]
