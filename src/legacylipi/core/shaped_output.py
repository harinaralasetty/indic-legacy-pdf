"""Paint HarfBuzz glyphs with logical Unicode encoded in a per-run CMap."""

import hashlib
from itertools import groupby
from pathlib import Path


def _insert_run(page, x, y, text, fontsize, font_path, color):
    """Keep shaping and copy/search text independent, including ligatures.

    CIDs identify occurrences rather than GIDs: the same painted glyph may
    represent different logical substrings. Each cluster's Unicode is divided
    across its glyphs in logical order, with the remainder on the last glyph.
    """
    import uharfbuzz as hb

    data = Path(font_path).read_bytes()
    font = hb.Font(hb.Face(data))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    infos, positions = buf.glyph_infos, buf.glyph_positions
    if not infos:
        return
    if len(infos) > 65535:
        raise ValueError("Shaped output run exceeds the CID limit")
    doc = page.parent
    base = page.insert_font(
        fontname="ShapedBase" + hashlib.sha256(data).hexdigest()[:16], fontfile=font_path
    )
    child = int(doc.xref_get_key(base, "DescendantFonts")[1].lstrip("[").split()[0])

    def obj(value):
        ref = doc.get_new_xref()
        doc.update_object(ref, value)
        return ref

    def stream(value):
        ref = obj("<< >>")
        doc.update_stream(ref, value)
        return ref

    clusters = sorted({g.cluster for g in infos}) + [len(text)]
    ends = dict(zip(clusters, clusters[1:], strict=False))
    counts = {}
    for glyph in infos:
        counts[glyph.cluster] = counts.get(glyph.cluster, 0) + 1
    seen = {}
    logical = []
    for glyph in infos:
        start = glyph.cluster
        index = seen.get(start, 0)
        value = text[start : ends[start]]
        logical.append(value[index : index + 1] if index < counts[start] - 1 else value[index:])
        seen[start] = index + 1

    descendant = obj(doc.xref_object(child))
    mapping = stream(b"\0\0" + b"".join(g.codepoint.to_bytes(2, "big") for g in infos))
    doc.xref_set_key(descendant, "CIDToGIDMap", f"{mapping} 0 R")
    widths = " ".join(
        str(
            (
                p.x_advance
                + (positions[i + 1].x_offset if i + 1 < len(positions) else 0)
                - p.x_offset
            )
            * 1000
            / font.face.upem
        )
        for i, p in enumerate(positions)
    )
    doc.xref_set_key(descendant, "W", f"[1 [{widths}]]")
    cmap = [
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /ShapedUnicode def /CMapType 2 def",
        "1 begincodespacerange <0000> <FFFF> endcodespacerange",
    ]
    # PDF CMap sections are limited to 100 entries.
    for start in range(0, len(logical), 100):
        values = logical[start : start + 100]
        cmap.append(f"{len(values)} beginbfchar")
        cmap.extend(
            f"<{start + i + 1:04x}> <{value.encode('utf-16-be').hex()}>"
            for i, value in enumerate(values)
        )
        cmap.append("endbfchar")
    cmap.append("endcmap CMapName currentdict /CMap defineresource pop end end")
    cmap_ref = stream("\n".join(cmap).encode())
    root = obj(doc.xref_object(base))
    doc.xref_set_key(root, "DescendantFonts", f"[{descendant} 0 R]")
    doc.xref_set_key(root, "ToUnicode", f"{cmap_ref} 0 R")
    resources = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
    name = f"Shaped{root}"
    kind, value = doc.xref_get_key(resources, "Font")
    if kind == "xref":
        doc.xref_set_key(int(value.split()[0]), name, f"{root} 0 R")
    else:
        doc.xref_set_key(resources, f"Font/{name}", f"{root} 0 R")
    scale = fontsize / font.face.upem
    y = page.rect.height - y
    ops = [f"q {' '.join(str(c) for c in color)} rg BT /{name} {fontsize} Tf"]
    ops.append(f"1 0 0 1 {x + positions[0].x_offset * scale:.6f} {y:.6f} Tm")
    for i, position in enumerate(positions):
        # Occurrence-specific widths include the next glyph's horizontal
        # offset, so the pen follows the shaped origins without artificial
        # TJ word-gap hints. Vertical mark offsets use text rise.
        if not logical[i]:
            # A dotted circle or decomposed mark can add a painted glyph with
            # no logical codepoint. Empty ToUnicode serves pypdf; ActualText
            # suppresses MuPDF's fallback to the raw CID for that glyph only.
            ops.append("/Span << /ActualText <FEFF> >> BDC")
        ops.append(f"{position.y_offset * scale:.6f} Ts <{i + 1:04x}> Tj")
        if not logical[i]:
            ops.append("EMC")
    ops.append("ET Q")
    contents = page.get_contents() + [stream("\n".join(ops).encode())]
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{r} 0 R" for r in contents) + "]")

    return sum(p.x_advance for p in positions) * scale


def insert_shaped_text(page, x, y, text, fontsize, font_path, color):
    """Shape in the requested font, using the bundled font for missing Latin."""
    import uharfbuzz as hb

    primary = hb.Font(hb.Face(Path(font_path).read_bytes()))
    fallback = Path(__file__).parent.parent / "fonts/NotoSansDevanagari-Regular.ttf"
    # The Telugu fixture has no Latin letters. Preserve visible mixed text
    # instead of emitting .notdef boxes that merely extract as Latin.
    for missing_latin, chars in groupby(
        text, key=lambda c: ord(c) < 128 and primary.get_nominal_glyph(ord(c)) is None
    ):
        part = "".join(chars)
        selected = str(fallback) if missing_latin else font_path
        x += _insert_run(page, x, y, part, fontsize, selected, color)
