"""Reverse shaping for embedded Identity CID fonts without a Unicode mapping.

No proprietary fonts, extracted text, or persistent font caches are written here.
The recovered string is a canonical preimage, not proof of original spelling.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher
from itertools import product
from pathlib import Path
from typing import Any

import fitz

from legacylipi.core.models import BoundingBox, PDFDocument, PDFPage, TextBlock

VIRAMA = "\u0c4d"
REPLACEMENT = "\ufffd"
TELUGU_RANGE = range(0x0C00, 0x0C80)
CONSONANTS = tuple(
    chr(c) for c in range(0x0C15, 0x0C3A) if unicodedata.category(chr(c)) == "Lo"
) + tuple(chr(c) for c in range(0x0C58, 0x0C5B))
VOWELS = tuple(chr(c) for c in range(0x0C05, 0x0C15) if unicodedata.category(chr(c)) == "Lo") + (
    "\u0c60",
    "\u0c61",
)
SIGNS = tuple(
    chr(c)
    for c in (*range(0x0C3E, 0x0C4D), 0x0C62, 0x0C63)
    if unicodedata.category(chr(c)).startswith("M")
)
MODIFIERS = ("\u0c00", "\u0c01", "\u0c02", "\u0c03", "\u0c04")
# Semantic post-table names used by Noto; these are names, never font-specific GIDs.
SIGN_NAMES = dict(
    zip(
        (
            0x0C3E,
            0x0C3F,
            0x0C40,
            0x0C41,
            0x0C42,
            0x0C43,
            0x0C44,
            0x0C46,
            0x0C47,
            0x0C48,
            0x0C4A,
            0x0C4B,
            0x0C4C,
            0x0C4D,
            0x0C55,
            0x0C56,
            0x0C62,
            0x0C63,
        ),
        (
            "aa",
            "i",
            "ii",
            "u",
            "uu",
            "rvocalic",
            "rrvocalic",
            "e",
            "ee",
            "ai",
            "o",
            "oo",
            "au",
            "virama",
            "lengthmark",
            "ailengthmark",
            "lvocalic",
            "llvocalic",
        ),
        strict=True,
    )
)
_CACHE: dict[tuple[str, str], GlyphMap] = {}
CACHE_LIMIT = 4
_TELUGU_FONT_CACHE: dict[str, bool] = {}
FONT_VERDICT_CACHE_LIMIT = 128
VERIFICATION_NOTE = (
    "round-trip = internal consistency of reading order/whitespace/ambiguity; "
    "it does NOT verify that inferred cmap mappings are correct; inferred entries "
    "are listed in the report and were spot-checked visually against page renders "
    "for the Gautami sample only. The map and this check use the same shaping and "
    "cmap assumptions; inserted layout whitespace and order between runs are not checked. "
    "This recovery run does not perform a visual check."
)
MAX_ON_DEMAND_CONSONANTS = 6
_CONJUNCT = re.compile(rf"([{''.join(CONSONANTS)}](?:{VIRAMA}[{''.join(CONSONANTS)}]){{2,}})(.*)")


def _dependencies() -> tuple[Any, Any]:
    try:
        import uharfbuzz
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ImportError(
            "Glyph recovery requires optional dependencies: install 'legacylipi[glyph]'."
        ) from exc
    return uharfbuzz, TTFont


@dataclass
class GlyphIdFont:
    xref: int
    base_font_name: str
    pages: list[int]


def _reference(doc: Any, xref: int, key: str) -> int:
    kind, value = doc.xref_get_key(xref, key)
    return int(value.split()[0]) if kind == "xref" else 0


def _descendant(doc: Any, xref: int) -> int:
    value = doc.xref_get_key(xref, "DescendantFonts")[1]
    match = re.search(r"(\d+)\s+\d+\s+R", value)
    return int(match[1]) if match else 0


def _is_telugu_font(font_bytes: bytes) -> bool:
    """Verify script evidence in the font program; cache only its hash/verdict."""
    try:
        from fontTools import agl
        from fontTools.ttLib import TTFont
    except ImportError:
        # Even a cached verdict must not bypass the optional-dependency guard.
        return False
    digest = hashlib.sha256(font_bytes).hexdigest()
    if digest in _TELUGU_FONT_CACHE:
        return _TELUGU_FONT_CACHE[digest]
    verified = False
    try:
        with TTFont(io.BytesIO(font_bytes), lazy=True) as font:
            for tag in ("GSUB", "GPOS"):
                scripts = getattr(font[tag].table, "ScriptList", None) if tag in font else None
                if scripts and any(r.ScriptTag in ("tel2", "telu") for r in scripts.ScriptRecord):
                    verified = True
                    break
            if not verified and "cmap" in font:
                verified = any(
                    cp in TELUGU_RANGE
                    for table in font["cmap"].tables
                    if table.isUnicode()
                    for cp in table.cmap
                )
            if not verified:
                verified = any(
                    name.split(".")[0].endswith("telu")
                    or any(ord(char) in TELUGU_RANGE for char in agl.toUnicode(name))
                    for name in font.getGlyphOrder()
                )
    except Exception:
        # Malformed/unsupported programs cannot establish Telugu support.
        verified = False
    if len(_TELUGU_FONT_CACHE) >= FONT_VERDICT_CACHE_LIMIT:
        _TELUGU_FONT_CACHE.pop(next(iter(_TELUGU_FONT_CACHE)))
    _TELUGU_FONT_CACHE[digest] = verified
    return verified


def _candidate(doc: Any, xref: int) -> bool:
    if not xref or doc.xref_get_key(xref, "Subtype")[1] != "/Type0":
        return False
    if doc.xref_get_key(xref, "Encoding")[1] not in ("/Identity-H", "/Identity-V"):
        return False
    if doc.xref_get_key(xref, "ToUnicode")[0] != "null":
        return False
    descendant = _descendant(doc, xref)
    if not descendant:
        return False
    descriptor = _reference(doc, descendant, "FontDescriptor")
    if not descriptor:
        return False
    truetype = _reference(doc, descriptor, "FontFile2")
    opentype = _reference(doc, descriptor, "FontFile3")
    if (
        truetype
        and doc.xref_get_key(descendant, "Subtype")[1] == "/CIDFontType2"
        and doc.xref_is_stream(truetype)
    ):
        return _is_telugu_font(doc.xref_stream(truetype))
    if (
        opentype
        and doc.xref_get_key(opentype, "Subtype")[1] == "/OpenType"
        and doc.xref_is_stream(opentype)
    ):
        return _is_telugu_font(doc.xref_stream(opentype))
    return False


def find_glyph_id_fonts(pdf_path: str | Path) -> list[GlyphIdFont]:
    """Find Telugu-verified Identity-H/V fonts lacking ToUnicode.

    fontTools is required for verification; HarfBuzz is not used. Pages are
    1-indexed and contain nonempty text operands using the font, including Forms.
    Unsupported content is conservatively excluded from automatic detection.
    """
    found: dict[int, GlyphIdFont] = {}
    verdicts: dict[int, bool] = {}
    with fitz.open(pdf_path) as doc:
        for page in doc:
            names = {}
            for xref, _, _, name, *_ in page.get_fonts(full=True):
                if xref not in verdicts:
                    verdicts[xref] = _candidate(doc, xref)
                if verdicts[xref]:
                    names[xref] = name
            if not names:
                continue
            try:
                runs = _raw_runs(page, set(names))
            except ValueError:
                continue
            used = {xref for group in runs.values() for xref, _ in group}
            for xref in sorted(used):
                font = found.setdefault(xref, GlyphIdFont(xref, names[xref], []))
                font.pages.append(page.number + 1)
    return list(found.values())


def _canonical(text: str) -> tuple[int, bool, str]:
    return len(text), text != unicodedata.normalize("NFC", text), text


@dataclass
class GlyphMap:
    mapping: dict[tuple[int, ...], str] = field(default_factory=dict)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    cmap_repairs: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    build_seconds: float = 0
    on_demand_seconds: float = 0
    _consonants: list[str] = field(default_factory=list, repr=False)
    font_hash: str = ""
    script: str = "telugu"
    gsub_features: list[str] = field(default_factory=list)
    font_bytes: bytes = field(default=b"", repr=False)
    max_length: int = 0
    _collision_index: dict[tuple[int, ...], int] = field(default_factory=dict, repr=False)
    _font: Any = field(default=None, repr=False)

    def add(self, gids: tuple[int, ...], text: str) -> None:
        if not gids or 0 in gids:
            return
        previous = self.mapping.get(gids)
        if previous is not None and previous != text:
            if gids not in self._collision_index:
                self._collision_index[gids] = len(self.collisions)
                self.collisions.append({"gids": list(gids), "alternatives": [previous]})
            collision = self.collisions[self._collision_index[gids]]
            if text not in collision["alternatives"]:
                collision["alternatives"].append(text)
            text = min(previous, text, key=_canonical)
            collision["canonical"] = text
        self.mapping[gids] = text
        self.max_length = max(self.max_length, len(gids))


def _prepare_font(
    font_bytes: bytes,
) -> tuple[bytes, dict[int, str], list[dict[str, Any]], list[str]]:
    _, font_class = _dependencies()
    from fontTools import agl
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    font = font_class(io.BytesIO(font_bytes))
    cmap: dict[int, str] = {}
    for table in font["cmap"].tables:
        if table.isUnicode():
            cmap.update(table.cmap)
    original = set(cmap)
    repairs: list[dict[str, Any]] = []
    names = set(font.getGlyphOrder())
    for name in sorted(names):
        if "." in name or "_" in name:
            continue
        text = agl.toUnicode(name)
        if len(text) == 1 and ord(text) not in cmap:
            cmap[ord(text)] = name
    for cp, stem in SIGN_NAMES.items():
        name = stem + ("" if cp in (0x0C4D, 0x0C55, 0x0C56) else "vowelsign") + "telu"
        if cp not in cmap and name in names:
            cmap[cp] = name
    for cp in sorted(set(cmap) - original):
        repairs.append(
            {"codepoint": f"U+{cp:04X}", "glyph": cmap[cp], "source": "semantic glyph name"}
        )
    # Some PDF producers remove the post names as well. A densely anchored,
    # Unicode-ordered base block permits conservative gap interpolation. This
    # is an INFERENCE, exposed in the report; reshaping cannot prove semantics.
    anchors = sorted((cp, font.getGlyphID(name)) for cp, name in cmap.items() if cp in TELUGU_RANGE)
    base_anchors = [a for a in anchors if 0x0C05 <= a[0] <= 0x0C39]
    ordered = len(base_anchors) >= 20 and all(
        a[1] < b[1] for a, b in zip(base_anchors, base_anchors[1:], strict=False)
    )
    if ordered and "GSUB" in font and font["post"].formatType == 3:
        order = font.getGlyphOrder()
        occupied = {font.getGlyphID(name) for name in cmap.values()}
        for (left, first), (right, last) in zip(anchors, anchors[1:], strict=False):
            missing = []
            repertoire = "current Unicode"
            for database, label in (
                (unicodedata, "current Unicode"),
                (unicodedata.ucd_3_2_0, "Unicode 3.2"),
            ):
                proposed = [
                    cp for cp in range(left + 1, right) if database.category(chr(cp)) != "Cn"
                ]
                if proposed and len(proposed) <= 12 and last - first == len(proposed) + 1:
                    missing, repertoire = proposed, label
                    break
            if not missing:
                continue
            if any(gid in occupied for gid in range(first + 1, last)):
                continue
            for cp, gid in zip(missing, range(first + 1, last), strict=True):
                cmap[cp] = order[gid]
                repairs.append(
                    {
                        "codepoint": f"U+{cp:04X}",
                        "glyph": order[gid],
                        "source": "inferred Unicode-ordered cmap gap",
                        "repertoire": repertoire,
                        "anchors": [f"U+{left:04X}", f"U+{right:04X}"],
                    }
                )
    # Patch only an in-memory copy; the embedded bytes remain untouched.
    if repairs:
        table = CmapSubtable.newSubtable(12)
        table.platformID, table.platEncID, table.language = 3, 10, 0
        table.cmap = cmap
        font["cmap"].tables.append(table)
        out = io.BytesIO()
        font.save(out)
        font_bytes = out.getvalue()
    features = (
        sorted({r.FeatureTag for r in font["GSUB"].table.FeatureList.FeatureRecord})
        if "GSUB" in font
        else []
    )
    font.close()
    return font_bytes, cmap, repairs, features


def _shape(text: str, font: Any) -> tuple[int, ...]:
    hb, _ = _dependencies()
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer)
    return tuple(g.codepoint for g in buffer.glyph_infos)


def build_glyph_map(font_bytes: bytes, script: str = "telugu") -> GlyphMap:
    """Build a bounded in-memory reverse map, keyed by original font SHA-256.

    One/two consonant clusters include all supported signs/modifiers. Three
    consonants include all supported signs; modifiers compose as suffixes.
    Bracketed Unicode-ordered cmap gaps are inferred and explicitly reported.
    """
    hb, _ = _dependencies()
    if script != "telugu":
        raise ValueError(f"Unsupported script: {script}")
    digest = hashlib.sha256(font_bytes).hexdigest()
    key = digest, script
    if key in _CACHE:
        return _CACHE[key]
    start = time.perf_counter()
    prepared, cmap, repairs, features = _prepare_font(font_bytes)
    font = hb.Font(hb.Face(prepared))
    mapping = GlyphMap(
        font_hash=digest,
        font_bytes=prepared,
        cmap_repairs=repairs,
        gsub_features=features,
        _font=font,
    )
    for cp in (*TELUGU_RANGE, *range(32, 127), 0x0964, 0x0965):
        char = chr(cp)
        if unicodedata.category(char) == "Cn":
            continue
        if cp not in cmap:
            mapping.skipped.append(
                {
                    "codepoint": f"U+{cp:04X}",
                    "reason": "No cmap entry or recoverable semantic glyph name",
                }
            )
            continue
        # Marks alone may produce a dotted circle; enumerate in syllables below.
        if not unicodedata.category(char).startswith("M"):
            mapping.add(_shape(char, font), char)
    consonants = [c for c in CONSONANTS if ord(c) in cmap]
    mapping._consonants = consonants
    signs = [""] + [c for c in SIGNS if ord(c) in cmap]
    modifiers = [""] + [c for c in MODIFIERS if ord(c) in cmap]
    for vowel in VOWELS:
        if ord(vowel) in cmap:
            for modifier in modifiers:
                mapping.add(_shape(vowel + modifier, font), vowel + modifier)
    for size in (1, 2, 3):
        if size > 1 and ord(VIRAMA) not in cmap:
            continue
        for cluster in product(consonants, repeat=size):
            stem = VIRAMA.join(cluster)
            endings = signs + ([VIRAMA] if ord(VIRAMA) in cmap else [])
            for sign in endings:
                text = stem + sign
                mapping.add(_shape(text, font), text)
                if size < 3 and sign != VIRAMA:
                    for modifier in modifiers[1:]:
                        mapping.add(_shape(text + modifier, font), text + modifier)
    # These are suffixes, not standalone well-formed syllables. No dotted circle.
    _, font_class = _dependencies()
    ttfont = font_class(io.BytesIO(prepared))
    for modifier in modifiers[1:]:
        mapping.add((ttfont.getGlyphID(cmap[ord(modifier)]),), modifier)
    ttfont.close()
    mapping.build_seconds = time.perf_counter() - start
    if len(_CACHE) >= CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = mapping
    return mapping


@dataclass
class DecodeResult:
    text: str
    unknown_count: int
    total_glyphs: int


def _extend_conjunct(
    gids: tuple[int, ...], start: int, text: str, length: int, mapping: GlyphMap
) -> tuple[str, int]:
    """Search bounded rare extensions only when an enumerated prefix matches.

    Every accepted extension must independently shape to a longer exact prefix.
    This avoids enumerating millions of unused four-to-six-consonant clusters.
    """
    match = _CONJUNCT.fullmatch(text)
    if not match or mapping._font is None or start + length == len(gids):
        return text, length
    started = time.perf_counter()
    stem, suffix = match.groups()
    frontier = [(stem, length)]
    best = text, length
    while frontier:
        stem, consumed = frontier.pop()
        if stem.count(VIRAMA) + 1 >= MAX_ON_DEMAND_CONSONANTS:
            continue
        for consonant in mapping._consonants:
            extended = stem + VIRAMA + consonant
            proposed = extended + suffix
            shaped = _shape(proposed, mapping._font)
            if len(shaped) <= consumed or shaped != gids[start : start + len(shaped)]:
                continue
            mapping.add(shaped, proposed)
            chosen = mapping.mapping[shaped]
            if len(shaped) > best[1] or (
                len(shaped) == best[1] and _canonical(chosen) < _canonical(best[0])
            ):
                best = chosen, len(shaped)
            frontier.append((extended, len(shaped)))
    mapping.on_demand_seconds += time.perf_counter() - started
    return best


def decode_glyph_stream(gids: Sequence[int], glyph_map: GlyphMap) -> DecodeResult:
    """Greedy longest exact GID match; every unknown GID yields one U+FFFD."""
    values = tuple(gids)
    text = []
    unknown = 0
    pos = 0
    while pos < len(values):
        for length in range(min(glyph_map.max_length, len(values) - pos), 0, -1):
            match = glyph_map.mapping.get(values[pos : pos + length])
            if match is not None:
                match, length = _extend_conjunct(values, pos, match, length, glyph_map)
                text.append(match)
                pos += length
                break
        else:
            text.append(REPLACEMENT)
            unknown += 1
            pos += 1
    return DecodeResult("".join(text), unknown, len(values))


@dataclass
class RoundtripReport:
    """Internal shaping consistency, not validation of inferred cmap semantics.

    match_ratio compares GIDs under the same cmap assumptions as the reverse map.
    It can expose within-run order/whitespace/ambiguity mismatches, but cannot
    establish correct reading order across runs or correctness of inferred text.
    """

    match_ratio: float
    matching_glyphs: int
    compared_glyphs: int
    mismatching_spans: list[dict[str, Any]]
    well_formedness_ratio: float
    well_formedness_issues: list[dict[str, Any]]


def _well_formedness(text: str) -> tuple[float, list[dict[str, Any]]]:
    issues = []
    words = list(re.finditer(r"\S+", text))
    for word in words:
        value = word[0]
        reasons = []
        if REPLACEMENT in value:
            reasons.append("replacement character")
        if unicodedata.category(value[0]).startswith("M"):
            reasons.append("word starts with a combining mark")
        # Terminal virama is legal in loanwords/abbreviations; flag for review,
        # not a grammatical verdict. Telugu orthography is not validated here.
        if value.endswith(VIRAMA):
            reasons.append("terminal virama (may be legitimate)")
        if reasons:
            issues.append({"start": word.start(), "end": word.end(), "reasons": reasons})
    return (1 - len(issues) / len(words) if words else 1), issues


def verify_roundtrip(
    recovered_text: str, original_gids: Sequence[int], font: GlyphMap | bytes
) -> RoundtripReport:
    """Reshape the complete recovered run under the reverse map's assumptions.

    This is internal consistency, not independent verification of inferred cmap
    mappings: shaping built the map in the first place. The denominator is max(input, output
    glyph count); insertions and deletions both reduce the ratio. Compare PDF
    runs separately so inferred layout whitespace never inflates the score.
    """
    hb, _ = _dependencies()
    if isinstance(font, GlyphMap):
        hb_font = font._font
    else:
        prepared, _, _, _ = _prepare_font(font)
        hb_font = hb.Font(hb.Face(prepared))
    reshaped = _shape(recovered_text, hb_font)
    original = tuple(original_gids)
    # A missing-glyph box is not evidence of recovered text, even if both
    # streams contain .notdef. Use an impossible GID only for alignment.
    comparable = tuple(gid if gid else -1 for gid in reshaped)
    matcher = SequenceMatcher(a=original, b=comparable, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    mismatches = [
        {
            "original_start": i,
            "original_end": j,
            "recovered_start": a,
            "recovered_end": b,
            "original_gids": list(original[i:j]),
            "reshaped_gids": list(reshaped[a:b]),
        }
        for op, i, j, a, b in matcher.get_opcodes()
        if op != "equal"
    ]
    denominator = max(len(original), len(reshaped))
    ratio, issues = _well_formedness(recovered_text)
    return RoundtripReport(
        matched / denominator if denominator else 1, matched, denominator, mismatches, ratio, issues
    )


@dataclass
class RecoveryResult:
    text: str
    report: dict[str, Any]
    pages: list[PDFPage] = field(default_factory=list)


def _tokens(data: bytes) -> list[Any]:
    """Lex PDF strings without interpreting escaped bytes as operators.

    Inline images are explicitly unsupported rather than scanning their binary
    payload as text. Image XObjects are supported (and ignored).
    """
    result: list[Any] = []
    pos = 0
    whitespace = b"\x00\t\n\x0c\r "
    delimiters = whitespace + b"()<>[]{}/%"
    while pos < len(data):
        char = data[pos]
        if char in whitespace:
            pos += 1
        elif char == 37:  # comment
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
        elif char == 40:
            value = bytearray()
            depth = 1
            pos += 1
            while pos < len(data) and depth:
                char = data[pos]
                pos += 1
                if char == 92:
                    if pos >= len(data):
                        raise ValueError("Truncated PDF string escape")
                    char = data[pos]
                    pos += 1
                    if char in b"01234567":
                        octal = bytes([char])
                        while len(octal) < 3 and pos < len(data) and data[pos] in b"01234567":
                            octal += data[pos : pos + 1]
                            pos += 1
                        value.append(int(octal, 8) & 255)
                    elif char in b"\r\n":
                        if char == 13 and data[pos : pos + 1] == b"\n":
                            pos += 1
                    else:
                        value.append({110: 10, 114: 13, 116: 9, 98: 8, 102: 12}.get(char, char))
                elif char == 40:
                    depth += 1
                    value.append(char)
                elif char == 41:
                    depth -= 1
                    if depth:
                        value.append(char)
                elif char == 13:
                    value.append(10)
                    if data[pos : pos + 1] == b"\n":
                        pos += 1
                else:
                    value.append(char)
            if depth:
                raise ValueError("Unterminated PDF literal string")
            result.append(bytes(value))
        elif char == 60 and data[pos : pos + 2] != b"<<":
            end = data.find(b">", pos + 1)
            if end < 0:
                raise ValueError("Unterminated PDF hex string")
            value = re.sub(rb"\s+", b"", data[pos + 1 : end])
            if len(value) % 2:
                value += b"0"
            result.append(bytes.fromhex(value.decode("ascii")))
            pos = end + 1
        elif data[pos : pos + 2] in (b"<<", b">>"):
            result.append(data[pos : pos + 2].decode("ascii"))
            pos += 2
        elif char in b"[]{}":
            result.append(chr(char))
            pos += 1
        else:
            start = pos
            pos += 1
            while pos < len(data) and data[pos] not in delimiters:
                pos += 1
            token = data[start:pos].decode("latin1")
            if token == "BI":
                raise ValueError(
                    "Inline image content is not supported; use image XObjects or a PDF without inline images"
                )
            if token.startswith("/"):
                token = re.sub(r"#([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), token)
            result.append(token)
    return result


def _raw_runs(page: Any, candidates: set[int]) -> dict[str, list[tuple[int, list[int]]]]:
    """Read Identity string operands by font resource, including nested Forms.

    MuPDF resolves resources and supplies positions; raw bytes are authoritative
    for CIDs, with CIDToGIDMap applied before the reverse map is consulted.
    """
    doc = page.parent
    resources: dict[int, dict[str, int]] = {}
    names = {}
    for xref, _, _, name, resource, _, owner in page.get_fonts(full=True):
        resources.setdefault(owner, {})[resource] = xref
        names[xref] = name.split("+")[-1]
    forms: dict[int, dict[str, int]] = {}
    for xref, name, owner, *_ in page.get_xobjects():
        forms.setdefault(owner, {})[name] = xref
    runs: dict[str, list[tuple[int, list[int]]]] = {}
    cid_maps = {}
    for xref in candidates:
        descendant = _descendant(doc, xref)
        ref = _reference(doc, descendant, "CIDToGIDMap")
        cid_maps[xref] = doc.xref_stream(ref) if ref else None
    used: set[int] = set()

    def visit(
        data: bytes,
        owner: int,
        inherited: dict[str, int],
        active: tuple[int, ...],
        font_xref: int = 0,
    ) -> None:
        if len(active) > 32:
            raise ValueError("Excessively nested PDF Form resources")
        fonts = inherited | resources.get(owner, {})
        args: list[Any] = []
        stack: list[int] = []
        for token in _tokens(data):
            if (
                isinstance(token, bytes)
                or token.startswith("/")
                or token in ("[", "]", "<<", ">>")
                or re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", token)
            ):
                args.append(token)
                continue
            if token == "Tf":
                font_xref = fonts.get(args[-2][1:], 0)
            elif token == "q":
                stack.append(font_xref)
            elif token == "Q":
                font_xref = stack.pop() if stack else 0
            elif token == "Do" and args:
                target = forms.get(owner, {}).get(args[-1][1:])
                if target:
                    if target in active:
                        raise ValueError("Cyclic PDF Form resources")
                    visit(doc.xref_stream(target), target, fonts, (*active, target), font_xref)
            elif token in ("Tj", "TJ", "'", '"'):
                raw = b"".join(a for a in args if isinstance(a, bytes))
                if raw:
                    used.add(font_xref)
                if font_xref not in candidates:
                    args = []
                    continue
                if len(raw) % 2:
                    raise ValueError("Identity text operand has an odd byte count")
                cids = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]
                cid_map = cid_maps[font_xref]
                gids = (
                    [int.from_bytes(cid_map[2 * c : 2 * c + 2], "big") for c in cids]
                    if cid_map is not None
                    else cids
                )
                if gids:
                    runs.setdefault(names[font_xref], []).append((font_xref, gids))
            args = []

    visit(page.read_contents(), 0, {}, ())
    # Only drawn fonts can cause ambiguity when joining raw runs to trace spans.
    # Unused resources often duplicate a candidate's name and are harmless.
    for xref in candidates & used:
        if any(names.get(other) == names[xref] for other in used - candidates):
            raise ValueError("Ambiguous font name shared by candidate and ordinary fonts")
    return runs


def _positioned_runs(page: Any, candidates: set[int]) -> list[dict[str, Any]]:
    raw = _raw_runs(page, candidates)
    offsets = {name: [0, 0] for name in raw}
    spans = []
    for span in page.get_texttrace():
        if not span["chars"]:
            continue
        name = span["font"].split("+")[-1]
        if name not in raw:
            spans.append(
                {
                    "xref": 0,
                    "text": "".join(chr(c[0]) if c[0] >= 0 else REPLACEMENT for c in span["chars"]),
                    "gids": [],
                    "x": span["bbox"][0],
                    "end": span["bbox"][2],
                    "y": span["chars"][0][2][1],
                    "size": span["size"],
                    "bbox": span["bbox"],
                    "font_name": span["font"],
                }
            )
            continue
        index, offset = offsets[name]
        pos = 0
        chars = span["chars"]
        while pos < len(chars):
            if index >= len(raw[name]):
                raise ValueError("PDF raw text and MuPDF trace have different glyph counts")
            xref, gids = raw[name][index]
            count = min(len(chars) - pos, len(gids) - offset)
            segment = chars[pos : pos + count]
            values = gids[offset : offset + count]
            if values != [c[1] for c in segment]:
                raise ValueError(
                    "Raw CID/GID sequence differs from MuPDF trace; cannot safely associate font resources"
                )
            spans.append(
                {
                    "xref": xref,
                    "gids": values,
                    "text": "",
                    "x": segment[0][2][0],
                    "end": max(c[3][2] for c in segment),
                    "y": segment[0][2][1],
                    "size": span["size"],
                    "bbox": (
                        min(c[3][0] for c in segment),
                        min(c[3][1] for c in segment),
                        max(c[3][2] for c in segment),
                        max(c[3][3] for c in segment),
                    ),
                    "font_name": span["font"],
                }
            )
            pos += count
            offset += count
            if offset == len(gids):
                index += 1
                offset = 0
        offsets[name] = [index, offset]
    for name, (index, offset) in offsets.items():
        if index != len(raw[name]) or offset:
            raise ValueError("Some raw PDF glyphs have no MuPDF text positions")
    return spans


def _lines(spans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Approximate reading order: baseline rows top-to-bottom, x left-to-right."""
    lines: list[list[dict[str, Any]]] = []
    for span in sorted(spans, key=lambda s: (s["y"], s["x"])):
        if lines and abs(lines[-1][0]["y"] - span["y"]) <= max(2, span["size"] * 0.3):
            lines[-1].append(span)
        else:
            lines.append([span])
    for line in lines:
        line.sort(key=lambda s: s["x"])
    return lines


def recover_pdf(pdf_path: str | Path, *, page_numbers: set[int] | None = None) -> RecoveryResult:
    """Recover candidate fonts; metrics count only glyph-ID fonts, per page/run."""
    _dependencies()
    candidates = find_glyph_id_fonts(pdf_path)
    if not candidates:
        raise ValueError(
            "No used Telugu-verified glyph-ID fonts found (Identity-H/V without ToUnicode)."
        )
    pages = []
    texts = []
    recovered_pages = []
    if page_numbers is not None:
        candidates = [f for f in candidates if page_numbers.intersection(f.pages)]
    with fitz.open(pdf_path) as doc:
        maps = {f.xref: build_glyph_map(doc.extract_font(f.xref)[3]) for f in candidates}
        for page in doc:
            if page_numbers is not None and page.number + 1 not in page_numbers:
                continue
            spans = _positioned_runs(
                page, {f.xref for f in candidates if page.number + 1 in f.pages}
            )
            page_report: dict[str, Any] = {
                "page": page.number + 1,
                "total_glyphs": 0,
                "unknown_count": 0,
                "matching_glyphs": 0,
                "compared_glyphs": 0,
                "runs": [],
            }
            lines = []
            blocks = []
            for line in _lines(spans):
                merged: list[dict[str, Any]] = []
                for span in line:
                    if (
                        merged
                        and merged[-1]["xref"] == span["xref"]
                        and span["x"] - merged[-1]["end"] <= max(1, span["size"] * 0.15)
                    ):
                        merged[-1]["gids"].extend(span["gids"])
                        merged[-1]["text"] += span["text"]
                        merged[-1]["end"] = max(merged[-1]["end"], span["end"])
                    else:
                        merged.append(dict(span, gids=list(span["gids"])))
                parts = []
                previous = None
                for span in merged:
                    if span["xref"]:
                        mapping = maps[span["xref"]]
                        decoded = decode_glyph_stream(span["gids"], mapping)
                        verified = verify_roundtrip(decoded.text, span["gids"], mapping)
                        text = decoded.text
                        page_report["total_glyphs"] += decoded.total_glyphs
                        page_report["unknown_count"] += decoded.unknown_count
                        page_report["matching_glyphs"] += verified.matching_glyphs
                        page_report["compared_glyphs"] += verified.compared_glyphs
                        page_report["runs"].append(
                            {
                                "font_xref": span["xref"],
                                "x": span["x"],
                                "y": span["y"],
                                "total_glyphs": decoded.total_glyphs,
                                "unknown_count": decoded.unknown_count,
                                **asdict(verified),
                            }
                        )
                    else:
                        text = span["text"]
                    if (
                        previous is not None
                        and parts
                        and not parts[-1].endswith(" ")
                        and not text.startswith(" ")
                    ):
                        gap = span["x"] - previous["end"]
                        if gap > max(1, span["size"] * 0.15):
                            parts.append("\t" if gap > span["size"] * 2 else " ")
                    parts.append(text)
                    previous = span
                line_text = "".join(parts).rstrip()
                lines.append(line_text)
                # One block per visual baseline row, sharing the text recovery's
                # reading order and whitespace. Keep the leading run's font.
                blocks.append(
                    TextBlock(
                        raw_text=line_text,
                        unicode_text=line_text,
                        position=BoundingBox(
                            min(s["bbox"][0] for s in line),
                            min(s["bbox"][1] for s in line),
                            max(s["bbox"][2] for s in line),
                            max(s["bbox"][3] for s in line),
                        ),
                        font_name=line[0]["font_name"],
                        font_size=line[0]["size"],
                        detected_encoding="glyph-id" if any(s["xref"] for s in line) else "unicode",
                    )
                )
            recovered_pages.append(
                PDFPage(page.number + 1, blocks, page.rect.width, page.rect.height)
            )
            page_text = "\n".join(lines)
            page_report["roundtrip_match_ratio"] = (
                page_report["matching_glyphs"] / page_report["compared_glyphs"]
                if page_report["compared_glyphs"]
                else 1
            )
            ratio, issues = _well_formedness(page_text)
            page_report["well_formedness_ratio"] = ratio
            page_report["well_formedness_issues"] = issues
            texts.append(page_text)
            pages.append(page_report)
    text = "\n\f\n".join(texts) + "\n"
    total = sum(p["total_glyphs"] for p in pages)
    unknown = sum(p["unknown_count"] for p in pages)
    compared = sum(p["compared_glyphs"] for p in pages)
    report = {
        "source": str(pdf_path),
        "script": "telugu",
        "verification_note": VERIFICATION_NOTE,
        "total_glyphs": total,
        "unknown_count": unknown,
        "glyph_coverage": 1 - unknown / total if total else 1,
        "roundtrip_match_ratio": sum(p["matching_glyphs"] for p in pages) / compared
        if compared
        else 1,
        "well_formedness_ratio": _well_formedness(text)[0],
        "collisions": sum(len(m.collisions) for m in maps.values()),
        "inferred_cmap_entries": sum(
            sum(r["source"].startswith("inferred") for r in m.cmap_repairs) for m in maps.values()
        ),
        "map_build_seconds": sum(
            m.build_seconds for m in {m.font_hash: m for m in maps.values()}.values()
        ),
        "fonts": [
            {
                **asdict(f),
                "font_hash": maps[f.xref].font_hash,
                "map_entries": len(maps[f.xref].mapping),
                "build_seconds": maps[f.xref].build_seconds,
                "on_demand_seconds": maps[f.xref].on_demand_seconds,
                "collisions": maps[f.xref].collisions,
                "cmap_repairs": maps[f.xref].cmap_repairs,
                "skipped": maps[f.xref].skipped,
                "gsub_features": maps[f.xref].gsub_features,
            }
            for f in candidates
        ],
        "pages": pages,
        "limitations": [
            "Glyph equivalence does not establish original spelling or linguistic accuracy.",
            "Reading order uses approximate horizontal baseline rows, not semantic table/column reconstruction.",
            "Scores cover candidate fonts only; ordinary fonts retain MuPDF Unicode extraction.",
            "Some cmap repairs infer Unicode order between known anchors; see cmap_repairs and skipped codepoints.",
            "Terminal virama is flagged for review, including legitimate loanwords and abbreviations.",
        ],
    }
    return RecoveryResult(text, report, recovered_pages)


def recover_document(document: PDFDocument, page_numbers: set[int]) -> PDFDocument:
    """Recover selected pages as positioned blocks; retain all other pages.

    Blocks follow the same approximate visual lines as ``recover_pdf``. Each
    uses the union of run bounds and the leading run's font name/size. Both text
    fields contain recovered Unicode because raw glyph codes are not Unicode.
    """
    if not page_numbers:
        return document
    result = recover_pdf(document.filepath, page_numbers=page_numbers)
    pages = {page.page_number: page for page in result.pages}
    return replace(document, pages=[pages.get(page.page_number, page) for page in document.pages])
