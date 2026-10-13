"""Offline OFL Telugu PDF fixtures shared by recovery and pipeline tests."""

import io
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont

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
