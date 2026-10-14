"""Reverse shaping for embedded Identity CID fonts without a Unicode mapping.

No proprietary fonts, extracted text, or persistent font caches are written here.
The recovered string is a canonical preimage, not proof of original spelling.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import sys
import threading
import time
import unicodedata
import zlib
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
CACHE_LIMIT = 1
_CACHE_LOCK = threading.RLock()
MAX_OPERATIONS_PER_PAGE = 2_000_000
MAX_OPERATIONS = 20_000_000
MAX_FORM_EXPANSIONS = 4096
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_FONTS = 8
MAX_ALIGNMENT_WORK = 4_000_000
MAX_RUN_ALIGNMENT_WORK = 250_000
WORD_GAP_EM = 0.15
UNCERTAIN_GAP_EM = (0.10, 0.25)
COLUMN_GAP_EM = 2.0
MAX_MAP_BYTES = 768 * 1024 * 1024


class RecoveryBudgetError(ValueError):
    """A controlled refusal, never a silent non-candidate result."""


@dataclass
class _Budget:
    page_count: int = 1
    operations: int = 0
    expansions: int = 0
    decoded: int = 0
    alignment_work: int = 0
    streams: dict[int, bytes] = field(default_factory=dict)
    font_programs: dict[str, bytes] = field(default_factory=dict)

    def stream(self, doc: Any, xref: int, *, font_program: bool = False) -> bytes:
        if xref in self.streams:
            return self.streams[xref]
        length_kind, length = doc.xref_get_key(xref, "Length")
        if length_kind == "int" and int(length) > MAX_STREAM_BYTES:
            raise RecoveryBudgetError("Encoded stream byte budget exceeded")
        raw = doc.xref_stream_raw(xref)
        kind, filter_name = doc.xref_get_key(xref, "Filter")
        # A duplicate font can still be decoded within the per-stream cap;
        # its bytes are charged only after content-hash deduplication below.
        decode_limit = (
            MAX_STREAM_BYTES
            if font_program
            else min(MAX_STREAM_BYTES, MAX_TOTAL_BYTES - self.decoded)
        )
        if kind == "null":
            data = raw
        elif filter_name in ("/FlateDecode", "[/FlateDecode]"):
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(raw, decode_limit + 1)
            except zlib.error as exc:
                raise ValueError("Malformed compressed PDF stream") from exc
            if decoder.unconsumed_tail or not decoder.eof:
                raise RecoveryBudgetError("Decoded stream byte budget exceeded or truncated stream")
        else:
            # Use MuPDF's streaming decoders, reading at most the remaining
            # allowance plus one sentinel byte (never xref_stream's unbounded
            # materialization). Filters are composed in PDF decode order.
            from pymupdf import mupdf

            filters = re.findall(r"/[A-Za-z0-9]+", filter_name)
            supported = {"/ASCIIHexDecode", "/ASCII85Decode", "/FlateDecode", "/LZWDecode"}
            if not filters or any(f not in supported for f in filters):
                raise ValueError(f"Unsupported stream filter for bounded recovery: {filter_name}")
            params_kind, params = doc.xref_get_key(xref, "DecodeParms")
            if params_kind not in ("null", "dict"):
                raise ValueError("Unsupported filter DecodeParms array")
            if params_kind == "dict" and re.search(r"/Predictor\s+(?!1\b)\d+", params):
                raise ValueError("Unsupported stream predictor")
            early = re.search(r"/EarlyChange\s+([01])", params)
            backing = mupdf.fz_new_buffer_from_copied_data(raw)
            chain = mupdf.fz_open_buffer(backing)
            try:
                for name in filters:
                    if name == "/ASCIIHexDecode":
                        chain = mupdf.fz_open_ahxd(chain)
                    elif name == "/ASCII85Decode":
                        chain = mupdf.fz_open_a85d(chain)
                    elif name == "/FlateDecode":
                        chain = mupdf.fz_open_flated(chain, 15)
                    else:
                        chain = mupdf.fz_open_lzwd(chain, int(early[1]) if early else 1, 9, 0, 0)
                limit = decode_limit
                output = bytearray()
                while len(output) <= limit:
                    chunk = bytearray(min(65536, limit + 1 - len(output)))
                    count = mupdf.fz_read(
                        chain, mupdf.python_mutable_buffer_data(chunk), len(chunk)
                    )
                    if not count:
                        break
                    output.extend(chunk[:count])
                data = bytes(output)
            except Exception as exc:
                raise ValueError("Malformed compressed PDF stream") from exc
        if len(data) > MAX_STREAM_BYTES:
            raise RecoveryBudgetError("Decoded stream byte budget exceeded")
        if font_program:
            digest = hashlib.sha256(data).hexdigest()
            if digest in self.font_programs:
                data = self.font_programs[digest]
            else:
                self.font_programs[digest] = data
                self.decoded += len(data)
        else:
            self.decoded += len(data)
        if self.decoded > MAX_TOTAL_BYTES:
            raise RecoveryBudgetError("Total decoded byte budget exceeded")
        self.streams[xref] = data
        return data

    def operation(self) -> None:
        self.operations += 1
        if self.operations > min(MAX_OPERATIONS_PER_PAGE * self.page_count, MAX_OPERATIONS):
            raise RecoveryBudgetError("Content operation budget exceeded")

    def expand(self) -> None:
        self.expansions += 1
        if self.expansions > MAX_FORM_EXPANSIONS:
            raise RecoveryBudgetError("Form expansion budget exceeded")


_TELUGU_FONT_CACHE: dict[str, bool] = {}
_TELUGU_FONT_CACHE_LOCK = threading.Lock()
FONT_VERDICT_CACHE_LIMIT = 128
VERIFICATION_NOTE = (
    "round-trip = internal consistency of reading order/whitespace/ambiguity; "
    "it does NOT verify that inferred cmap mappings are correct; inferred entries "
    "are listed in the report. Manual comparison was done once on one page region "
    "by a reviewer (Gautami sample only), not reproducible from this repository. "
    "Repository evidence is synthetic ground-truth tests. The map and this check use the same shaping and "
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
    kind, value = doc.xref_get_key(xref, "DescendantFonts")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    match = re.fullmatch(r"\[\s*(\d+)\s+\d+\s+R\s*\]", value)
    if not match or doc.xref_get_key(int(match[1]), "Type")[1] != "/Font":
        raise ValueError("Invalid CID DescendantFonts: expected one font dictionary reference")
    return int(match[1])


def _is_telugu_font(font_bytes: bytes) -> bool:
    """Verify script evidence in the font program; cache only its hash/verdict."""
    try:
        from fontTools import agl
        from fontTools.ttLib import TTFont
    except ImportError:
        # Even a cached verdict must not bypass the optional-dependency guard.
        return False
    digest = hashlib.sha256(font_bytes).hexdigest()
    with _TELUGU_FONT_CACHE_LOCK:
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
    with _TELUGU_FONT_CACHE_LOCK:
        if digest not in _TELUGU_FONT_CACHE:
            while len(_TELUGU_FONT_CACHE) >= FONT_VERDICT_CACHE_LIMIT:
                _TELUGU_FONT_CACHE.pop(next(iter(_TELUGU_FONT_CACHE)))
            _TELUGU_FONT_CACHE[digest] = verified
    return verified


def _candidate(doc: Any, xref: int, budget: _Budget) -> bool:
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
        return _is_telugu_font(budget.stream(doc, truetype, font_program=True))
    if (
        opentype
        and doc.xref_get_key(opentype, "Subtype")[1] == "/OpenType"
        and doc.xref_is_stream(opentype)
    ):
        return _is_telugu_font(budget.stream(doc, opentype, font_program=True))
    return False


def _font_program_ref(doc: Any, xref: int) -> int:
    descriptor = _reference(doc, _descendant(doc, xref), "FontDescriptor")
    return _reference(doc, descriptor, "FontFile2") or _reference(doc, descriptor, "FontFile3")


def has_glyph_id_font_resources(pdf_path: str | Path) -> bool:
    """Check font resources only; never execute or decode content streams."""
    seen: set[int] = set()
    budget = _Budget()
    with fitz.open(pdf_path) as doc:
        for page in doc:
            for xref, *_ in page.get_fonts(full=True):
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    if _candidate(doc, xref, budget):
                        return True
                except (ValueError, RuntimeError):
                    continue
    return False


def find_glyph_id_fonts(pdf_path: str | Path, *, strict_fonts: bool = False) -> list[GlyphIdFont]:
    """Inspect used Identity fonts only; count distinct verified program hashes.

    Automatic detection excludes unsupported/undecodable programs. Explicit
    recovery reports their font names. Content execution retains hard budgets.
    """
    found: dict[int, GlyphIdFont] = {}
    verdicts: dict[int, bool] = {}
    hashes: set[str] = set()
    with fitz.open(pdf_path) as doc:
        budget = _Budget(page_count=doc.page_count)
        for page in doc:
            names = {
                xref: name
                for xref, _, _, name, *_ in page.get_fonts(full=True)
                if doc.xref_get_key(xref, "Subtype")[1] == "/Type0"
                and doc.xref_get_key(xref, "Encoding")[1] in ("/Identity-H", "/Identity-V")
                and doc.xref_get_key(xref, "ToUnicode")[0] == "null"
            }
            if not names:
                continue
            used: set[int] = set()
            try:
                _raw_runs(page, set(), budget=budget, discovery=True, _used_fonts=used)
            except RecoveryBudgetError:
                raise
            except ValueError:
                continue
            for xref in sorted(used & names.keys()):
                if xref not in verdicts:
                    try:
                        verdicts[xref] = _candidate(doc, xref, budget)
                    except (ValueError, RuntimeError) as exc:
                        if strict_fonts:
                            raise ValueError(
                                f"Cannot decode font {names[xref]} (xref {xref}): {exc}"
                            ) from exc
                        verdicts[xref] = False
                    if verdicts[xref]:
                        data = budget.stream(doc, _font_program_ref(doc, xref), font_program=True)
                        hashes.add(hashlib.sha256(data).hexdigest())
                        if len(hashes) > MAX_CANDIDATE_FONTS:
                            raise RecoveryBudgetError("Candidate font budget exceeded")
                if verdicts[xref]:
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
    estimated_bytes: int = 0

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
        if previous is None:
            # Conservative accounted size includes tuple, scalar IDs, string,
            # dictionary overhead and room for collision/extension bookkeeping.
            self.estimated_bytes += sys.getsizeof(gids) + sys.getsizeof(text) + 128 + 28 * len(gids)
            if self.estimated_bytes > MAX_MAP_BYTES:
                raise RecoveryBudgetError("Glyph map memory budget exceeded")
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
    # Serialize cache builds/mutations: cold requests for a key share one map;
    # different keys also serialize so construction cannot exceed the cache budget.
    with _CACHE_LOCK:
        return _build_glyph_map(font_bytes, script)


def _build_glyph_map(font_bytes: bytes, script: str = "telugu") -> GlyphMap:
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
    _CACHE.clear()  # Release the prior map before allocating a new one.
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
    warnings: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[tuple[int, int, str]] = field(default_factory=list)


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


def decode_glyph_stream(
    gids: Sequence[int], glyph_map: GlyphMap, *, advances: Sequence[float] | None = None
) -> DecodeResult:
    with _CACHE_LOCK:
        return _decode_glyph_stream(gids, glyph_map, advances=advances)


def _decode_glyph_stream(
    gids: Sequence[int], glyph_map: GlyphMap, *, advances: Sequence[float] | None = None
) -> DecodeResult:
    """Greedy longest exact GID match; every unknown GID yields one U+FFFD."""
    values = tuple(gids)
    text = []
    clusters = []
    unknown = 0
    warnings: list[dict[str, Any]] = []
    pos = 0
    while pos < len(values):
        for length in range(min(glyph_map.max_length, len(values) - pos), 0, -1):
            match = glyph_map.mapping.get(values[pos : pos + length])
            if match is not None:
                match, length = _extend_conjunct(values, pos, match, length, glyph_map)
                if match == " " and (
                    (advances is not None and abs(advances[pos]) < 0.01)
                    or (advances is None and text and text[-1].endswith(VIRAMA))
                ):
                    # Default ignorables can alias the space GID. Without an
                    # observable positive advance, do not invent a word boundary.
                    match = ""
                    warnings.append(
                        {
                            "code": "ambiguous-joiner",
                            "message": "Space GID with no observed advance may be a zero-width joiner; kept joiner-less text.",
                        }
                    )
                text.append(match)
                clusters.append((pos, pos + length, match))
                pos += length
                break
        else:
            text.append(REPLACEMENT)
            clusters.append((pos, pos + 1, REPLACEMENT))
            unknown += 1
            pos += 1
    return DecodeResult("".join(text), unknown, len(values), warnings, clusters)


@dataclass
class RoundtripReport:
    """Internal shaping consistency, not validation of inferred cmap semantics.

    match_ratio compares GIDs under the same cmap assumptions as the reverse map.
    It can expose within-run order/whitespace/ambiguity mismatches, but cannot
    establish correct reading order across runs or correctness of inferred text.
    """

    match_ratio: float | None
    matching_glyphs: int
    compared_glyphs: int
    mismatching_spans: list[dict[str, Any]]
    well_formedness_ratio: float
    well_formedness_issues: list[dict[str, Any]]
    verification_complete: bool = True


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
    recovered_text: str,
    original_gids: Sequence[int],
    font: GlyphMap | bytes,
    *,
    _budget: _Budget | None = None,
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
    mismatches = []
    if original == comparable:
        matched = len(original)
    else:
        work = len(original) * len(comparable)
        remaining = MAX_ALIGNMENT_WORK - (_budget.alignment_work if _budget else 0)
        if work > min(MAX_RUN_ALIGNMENT_WORK, remaining):
            ratio, issues = _well_formedness(recovered_text)
            return RoundtripReport(None, 0, 0, [], ratio, issues, verification_complete=False)
        if _budget is not None:
            _budget.alignment_work += work
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
    warnings: list[dict[str, Any]] = field(default_factory=list)
    inferred_codepoints: list[str] = field(default_factory=list)


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


def _cid_widths(doc: Any, xref: int, budget: _Budget) -> tuple[float, dict[int, float]]:
    """Validate CID widths before using advances for geometric word gaps."""
    child = _descendant(doc, xref)

    def number(value: Any, key: str, *, index: bool = False) -> float:
        if not isinstance(value, str) or not re.fullmatch(
            r"[+-]?\d+" if index else r"[+-]?(?:\d+\.?\d*|\.\d+)", value
        ):
            raise ValueError(
                f"Invalid CID {key}: expected {'integer index' if index else 'numeric width'}"
            )
        result = float(value)
        if not math.isfinite(result) or (index and not 0 <= result <= 65535):
            raise ValueError(f"Invalid CID {key}: number out of range")
        return result

    kind, value = doc.xref_get_key(child, "DW")
    if kind == "null":
        default = 1000.0
    elif kind in ("int", "float"):
        default = number(value, "DW")
    else:
        raise ValueError("Invalid CID DW: expected numeric default width")
    kind, value = doc.xref_get_key(child, "W")
    if kind == "null":
        return default, {}
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    elif kind != "array":
        raise ValueError("Invalid CID W: expected width array")
    if len(value) > MAX_STREAM_BYTES:
        raise RecoveryBudgetError("Font widths byte budget exceeded")
    tokens = _tokens(value.encode())
    if not tokens or tokens[0] != "[" or tokens[-1] != "]":
        raise ValueError("Invalid CID W: expected complete width array")
    tokens = tokens[1:-1]
    widths: dict[int, float] = {}
    i = 0
    while i < len(tokens):
        budget.operation()
        first = int(number(tokens[i], "W", index=True))
        if i + 1 >= len(tokens):
            raise ValueError("Invalid CID W: truncated width entry")
        if tokens[i + 1] == "[":
            i += 2
            cid = first
            while i < len(tokens) and tokens[i] != "]":
                budget.operation()
                if cid > 65535:
                    raise ValueError("Invalid CID W: index out of range")
                widths[cid] = number(tokens[i], "W")
                cid += 1
                i += 1
            if i == len(tokens):
                raise ValueError("Invalid CID W: unterminated width list")
            i += 1
        else:
            if i + 2 >= len(tokens):
                raise ValueError("Invalid CID W: truncated width range")
            last = int(number(tokens[i + 1], "W", index=True))
            width = number(tokens[i + 2], "W")
            if first > last:
                raise ValueError("Invalid CID W: reversed width range")
            for cid in range(first, last + 1):
                budget.operation()
                widths[cid] = width
            i += 3
    return default, widths


@dataclass
class _TextState:
    text_object: int = 0
    font_xref: int = 0
    mode: int = 0
    font_size: float = 0
    char_spacing: float = 0
    word_spacing: float = 0
    horizontal_scale: float = 1
    matrix: Any = field(default_factory=lambda: fitz.Matrix(1, 1))
    ctm: Any = field(default_factory=lambda: fitz.Matrix(1, 1))


# Width, tracking and em size in page coordinates for each painted glyph.
_RawRun = tuple[int, list[int], list[float], list[float], list[float], int]


def _raw_runs(
    page: Any,
    candidates: set[int],
    *,
    budget: _Budget | None = None,
    discovery: bool = False,
    _used_fonts: set[int] | None = None,
) -> dict[str, list[_RawRun]]:
    """Read bounded raw runs; discovery visits each Form/resource context once.

    Trace origins supply geometry; PDF text state supplies expected advances.
    Ts/TL/Td/TD affect trace baselines, not horizontal advances. Identity-H/V
    codes are two bytes, so Tw (which applies only to single-byte 0x20) never
    adds advance here, even for a CID/GID whose Unicode mapping is a space.
    """
    budget = budget or _Budget()
    doc = page.parent
    resources: dict[int, dict[str, int]] = {}
    names = {}
    for xref, _, _, name, resource, _, owner in page.get_fonts(full=True):
        resources.setdefault(owner, {})[resource] = xref
        names[xref] = name.split("+")[-1]
    forms: dict[int, dict[str, int]] = {}
    for xref, name, owner, *_ in page.get_xobjects():
        forms.setdefault(owner, {})[name] = xref
    runs: dict[str, list[_RawRun]] = {}
    cid_maps = {}
    widths = {xref: _cid_widths(doc, xref, budget) for xref in candidates}
    for xref in candidates:
        kind, value = doc.xref_get_key(_descendant(doc, xref), "CIDToGIDMap")
        if kind == "xref":
            ref = int(value.split()[0])
            if not doc.xref_is_stream(ref):
                raise ValueError("Invalid CIDToGIDMap: expected stream")
            data = budget.stream(doc, ref)
            cid_maps[xref] = data
        elif kind == "null" or (kind == "name" and value == "/Identity"):
            cid_maps[xref] = None
        else:
            raise ValueError("Invalid CIDToGIDMap: expected Identity or stream")
    used: set[int] = _used_fonts if _used_fonts is not None else set()
    visited: set[tuple[Any, ...]] = set()
    numeric = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")
    text_object = 0

    def visit(
        data: bytes,
        owner: int,
        inherited: dict[str, int],
        active: tuple[int, ...],
        state: _TextState | None = None,
    ) -> None:
        nonlocal text_object
        if len(active) > 32:
            raise RecoveryBudgetError("Form nesting budget exceeded")
        state = state or _TextState()
        fonts = inherited | resources.get(owner, {})
        key = owner, tuple(sorted(fonts.items())), state.font_xref, state.mode
        if discovery and key in visited:
            return
        visited.add(key)
        budget.expand()
        args: list[Any] = []
        stack: list[_TextState] = []
        for token in _tokens(data):
            budget.operation()  # Includes operands: bounds token and operator work.
            if (
                isinstance(token, bytes)
                or token.startswith("/")
                or token in ("[", "]", "<<", ">>")
                or numeric.fullmatch(token)
            ):
                args.append(token)
                continue
            arity = {
                "Tf": 2,
                "Tr": 1,
                "Tc": 1,
                "Tw": 1,
                "Tz": 1,
                "Ts": 1,
                "TL": 1,
                "Tm": 6,
                "Td": 2,
                "TD": 2,
                "Tj": 1,
                "TJ": 2,
                "'": 1,
                '"': 3,
                "Do": 1,
                "cm": 6,
            }
            if token in arity and len(args) != arity[token] and token != "TJ":
                raise ValueError(f"Malformed PDF operator {token}: operand count")
            if token in ("Tf", "Do") and (
                not args or not isinstance(args[0], str) or not args[0].startswith("/")
            ):
                raise ValueError(f"Malformed PDF operator {token}: font/resource name")
            numbers = args[1:] if token == "Tf" else args
            if token in ("Tf", "Tr", "Tc", "Tw", "Tz", "Ts", "TL", "Tm", "Td", "TD", "cm") and any(
                not isinstance(a, str) or not numeric.fullmatch(a) for a in numbers
            ):
                raise ValueError(f"Malformed PDF operator {token}: numeric operands")
            if token == "Tf":
                state.font_xref = fonts.get(args[0][1:], 0)
                state.font_size = float(args[1])
            elif token == "Tc":
                state.char_spacing = float(args[0])
            elif token == "Tw":
                state.word_spacing = float(args[0])
            elif token == "Tz":
                state.horizontal_scale = float(args[0]) / 100
            elif token == "BT":
                text_object += 1
                state.text_object = text_object
                state.matrix = fitz.Matrix(1, 1)
            elif token == "Tm":
                state.matrix = fitz.Matrix(*(float(a) for a in args))
            elif token == "cm":
                state.ctm = fitz.Matrix(*(float(a) for a in args)) * state.ctm
            elif token == "Tr":
                state.mode = int(float(args[0]))
                if state.mode not in range(8):
                    raise ValueError("Malformed PDF render mode")
            elif token == "q":
                stack.append(replace(state))
            elif token == "Q":
                # Text matrices are not part of the saved graphics state.
                matrix, object_id = state.matrix, state.text_object
                state = stack.pop() if stack else _TextState()
                state.matrix = matrix
                state.text_object = object_id
            elif token == "Do":
                target = forms.get(owner, {}).get(args[0][1:])
                if target:
                    if target in active:
                        raise ValueError("Cyclic PDF Form resources")
                    form_state = replace(state)
                    kind, value = doc.xref_get_key(target, "Matrix")
                    if kind != "null":
                        matrix = _tokens(value.encode())
                        if len(matrix) != 8 or matrix[0] != "[" or matrix[-1] != "]":
                            raise ValueError("Malformed Form Matrix")
                        form_state.ctm = fitz.Matrix(*(float(a) for a in matrix[1:-1])) * state.ctm
                    visit(
                        budget.stream(doc, target),
                        target,
                        fonts,
                        (*active, target),
                        form_state,
                    )
            elif token in ("Tj", "TJ", "'", '"'):
                if token == "TJ":
                    if (
                        len(args) < 2
                        or args[0] != "["
                        or args[-1] != "]"
                        or any(
                            not isinstance(a, bytes)
                            and (not isinstance(a, str) or not numeric.fullmatch(a))
                            for a in args[1:-1]
                        )
                    ):
                        raise ValueError("Malformed PDF operator TJ")
                elif not isinstance(args[-1], bytes):
                    raise ValueError(f"Malformed PDF operator {token}: text string")
                if token == '"':
                    state.word_spacing, state.char_spacing = float(args[0]), float(args[1])
                if state.mode in (3, 7):  # Invisible OCR and clipping-only are excluded.
                    args = []
                    continue
                chunks = [b"".join(arg for arg in args if isinstance(arg, bytes))]
                font_xref = state.font_xref
                if any(chunks):
                    used.add(font_xref)
                if font_xref in candidates:
                    for raw in chunks:
                        if len(raw) % 2:
                            raise ValueError("Identity text operand has an odd byte count")
                        cids = [
                            int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)
                        ]
                        cid_map = cid_maps[font_xref]
                        gids = (
                            [int.from_bytes(cid_map[2 * c : 2 * c + 2], "big") for c in cids]
                            if cid_map is not None
                            else cids
                        )
                        if gids:
                            default, table = widths[font_xref]
                            # Project the text advance through Tz, Tm and the CTM.
                            # Origins already include rise, leading and positioning.
                            scale = state.horizontal_scale * (state.matrix * state.ctm).a
                            em = abs(state.font_size * scale)
                            runs.setdefault(names[font_xref], []).append(
                                (
                                    font_xref,
                                    gids,
                                    [
                                        table.get(c, default) / 1000 * state.font_size * scale
                                        for c in cids
                                    ],
                                    [state.char_spacing * scale] * len(cids),
                                    [em] * len(cids),
                                    state.text_object,
                                )
                            )
            args = []
        if args:
            raise ValueError("Trailing PDF operands without operator")

    for content in page.get_contents():
        # Concatenation preserves graphics/text state across content streams.
        budget.stream(doc, content)
    visit(b"\n".join(budget.stream(doc, ref) for ref in page.get_contents()), 0, {}, ())
    for xref in candidates & used:
        if any(names.get(other) == names[xref] for other in used - candidates):
            raise ValueError("Ambiguous font name shared by candidate and ordinary fonts")
    return runs


def _positioned_runs(
    page: Any, candidates: set[int], budget: _Budget | None = None
) -> list[dict[str, Any]]:
    raw = _raw_runs(page, candidates, budget=budget)
    offsets = {name: [0, 0] for name in raw}
    spans = []
    previous_paint = None
    for span in page.get_texttrace():
        if span["type"] == 3:
            continue
        paint = (span["font"], span["chars"], span["bbox"])
        # MuPDF emits an immediately adjacent fill/stroke pair for Tr 2/6.
        if span["type"] == 1 and previous_paint == paint:
            previous_paint = None
            continue
        previous_paint = paint if span["type"] == 0 else None
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
            xref, gids, widths, tracking, sizes, text_object = raw[name][index]
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
                    "text_object": text_object,
                    "gids": values,
                    "origins": [c[2][0] for c in segment],
                    "widths": widths[offset : offset + count],
                    "tracking": tracking[offset : offset + count],
                    "sizes": sizes[offset : offset + count],
                    "advances": [
                        (chars[pos + i + 1][2][0] - c[2][0])
                        if pos + i + 1 < len(chars)
                        else c[3][2] - c[3][0]
                        for i, c in enumerate(segment)
                    ],
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
    rows: list[list[list[dict[str, Any]]]] = []
    # Trace fragments are not independent words: a positioned mark can move
    # left of the preceding glyph. Assemble spatially contiguous content-order
    # runs first, then sort ONLY those runs for reading order.
    runs: list[list[dict[str, Any]]] = []
    for span in spans:
        previous = runs[-1][-1] if runs else None
        if (
            previous is not None
            and span.get("text_object") == previous.get("text_object")
            and span["xref"] == previous["xref"]
            and span["font_name"] == previous["font_name"]
            and abs(span["y"] - previous["y"]) <= max(2, span["size"] * 0.3)
            and previous["x"] - span["size"] <= span["x"]
            and span["x"] <= previous["end"] + span["size"] * COLUMN_GAP_EM
        ):
            runs[-1].append(span)
        else:
            runs.append([span])
    ordered = sorted(runs, key=lambda run: (run[0]["y"], min(s["x"] for s in run)))
    for run in ordered:
        span = run[0]
        if rows and abs(rows[-1][0][0]["y"] - span["y"]) <= max(2, span["size"] * 0.3):
            rows[-1].append(run)
        else:
            rows.append([run])
    return [
        [span for run in sorted(row, key=lambda r: min(s["x"] for s in r)) for span in run]
        for row in rows
    ]


def _shaped_steps(decoded: DecodeResult, mapping: GlyphMap, span: dict[str, Any]) -> list[float]:
    """Expected origin deltas from candidate shaping, including GPOS offsets.

    PDF widths remain authoritative when a producer deliberately overrides the
    font metrics. The shaping correction is relative to nominal font advances;
    this also supports older PDFs that use a constant DW for all glyphs.
    """
    hb, _ = _dependencies()
    font = mapping._font
    upem = font.face.upem
    offsets = [0.0] * len(span["gids"])
    advances = list(span["widths"])
    for start, end, text in decoded.clusters:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf)
        if tuple(g.codepoint for g in buf.glyph_infos) != tuple(span["gids"][start:end]):
            continue
        for i, pos in enumerate(buf.glyph_positions, start):
            scale = span["sizes"][i] / upem
            nominal = font.get_glyph_h_advance(span["gids"][i])
            offsets[i] = pos.x_offset * scale
            advances[i] += (pos.x_advance - nominal) * scale
    return [advances[i] + offsets[i + 1] - offsets[i] for i in range(len(advances) - 1)]


def recover_pdf(
    pdf_path: str | Path,
    *,
    page_numbers: set[int] | None = None,
    strict_cmap: bool = False,
    convert_legacy: bool = False,
) -> RecoveryResult:
    """Recover visible text within aggregate budgets; disclose used cmap inference."""
    _dependencies()
    candidates = find_glyph_id_fonts(pdf_path, strict_fonts=True)
    if page_numbers is not None:
        candidates = [f for f in candidates if page_numbers.intersection(f.pages)]
    if not candidates:
        raise ValueError(
            "No used Telugu-verified glyph-ID fonts found (Identity-H/V without ToUnicode)."
        )
    warnings: list[dict[str, Any]] = []
    inferred: set[str] = set()
    font_reports = []
    page_data = []
    with fitz.open(pdf_path) as doc:
        budget = _Budget(page_count=doc.page_count)
        # Validate all expanded content before asking MuPDF for trace positions.
        # In particular this refuses exponential paint graphs before rendering.
        for page in doc:
            if page_numbers is not None and page.number + 1 not in page_numbers:
                continue
            spans = _positioned_runs(
                page, {f.xref for f in candidates if page.number + 1 in f.pages}, budget
            )
            if convert_legacy:
                from legacylipi.core.encoding_detector import EncodingDetector
                from legacylipi.core.unicode_converter import UnicodeConverter

                detector, converter = EncodingDetector(), UnicodeConverter()
                for span in spans:
                    if not span["xref"]:
                        match = detector.detect_from_text_block(
                            TextBlock(raw_text=span["text"], font_name=span["font_name"])
                        )
                        if match.is_legacy:
                            span["text"] = converter.convert_text(
                                span["text"], match.detected_encoding
                            ).converted_text
            lines = []
            for line in _lines(spans):
                merged: list[dict[str, Any]] = []
                for span in line:
                    if (
                        merged
                        and merged[-1]["xref"] == span["xref"]
                        and merged[-1]["font_name"] == span["font_name"]
                        and (
                            span["xref"]
                            or span["x"] - merged[-1]["end"] <= span["size"] * WORD_GAP_EM
                        )
                    ):
                        merged[-1]["gids"].extend(span["gids"])
                        for key in ("advances", "origins", "widths", "sizes", "tracking"):
                            merged[-1].setdefault(key, []).extend(span.get(key, []))
                        merged[-1]["text"] += span["text"]
                        merged[-1]["end"] = max(merged[-1]["end"], span["end"])
                        box = merged[-1]["bbox"]
                        merged[-1]["bbox"] = (
                            min(box[0], span["bbox"][0]),
                            min(box[1], span["bbox"][1]),
                            max(box[2], span["bbox"][2]),
                            max(box[3], span["bbox"][3]),
                        )
                    else:
                        merged.append(dict(span, gids=list(span["gids"])))
                lines.append(merged)
            page_data.append((page.number + 1, page.rect.width, page.rect.height, lines))
        # Group resources by program hash before map construction and traversal.
        # One map per distinct used font, regardless of page/resource copies.
        groups: dict[str, list[GlyphIdFont]] = {}
        programs: dict[str, bytes] = {}
        for candidate in candidates:
            data = budget.stream(doc, _font_program_ref(doc, candidate.xref), font_program=True)
            digest = hashlib.sha256(data).hexdigest()
            groups.setdefault(digest, []).append(candidate)
            programs[digest] = data
        for digest, group in groups.items():
            candidate = group[0]
            font_xrefs = {f.xref for f in group}
            mapping = build_glyph_map(programs[digest])
            inferred_entries = {
                r["codepoint"] for r in mapping.cmap_repairs if r["source"].startswith("inferred")
            }
            for number, _, _, lines in page_data:
                for line in lines:
                    for span in line:
                        if span["xref"] not in font_xrefs:
                            continue
                        origins = span["origins"]
                        advances = [
                            b - a - tracking
                            for a, b, tracking in zip(
                                origins, origins[1:], span["tracking"], strict=False
                            )
                        ]
                        advances.append(span["widths"][-1])
                        decoded = decode_glyph_stream(span["gids"], mapping, advances=advances)
                        used = {f"U+{ord(c):04X}" for c in decoded.text} & inferred_entries
                        if used and strict_cmap:
                            raise ValueError(
                                "Strict cmap refuses inferred mappings: " + ", ".join(sorted(used))
                            )
                        inferred.update(used)
                        warnings.extend(decoded.warnings)
                        expected_steps = _shaped_steps(decoded, mapping, span)
                        cluster_parts = []
                        for start, _end, text in decoded.clusters:
                            if (
                                start
                                and cluster_parts
                                and not cluster_parts[-1].endswith(" ")
                                and not text.startswith(" ")
                            ):
                                gap = (
                                    origins[start]
                                    - origins[start - 1]
                                    - expected_steps[start - 1]
                                    - span["tracking"][start - 1]
                                )
                                size = span["sizes"][start - 1]
                                if (
                                    size
                                    and UNCERTAIN_GAP_EM[0] <= gap / size <= UNCERTAIN_GAP_EM[1]
                                ):
                                    warnings.append(
                                        {
                                            "code": "spacing_ambiguity",
                                            "message": "A geometric gap is near the word boundary threshold; whitespace is uncertain.",
                                            "page": number,
                                            "font_xref": span["xref"],
                                            "glyph_index": start,
                                            "gap_em": gap / size,
                                        }
                                    )
                                if gap > size * WORD_GAP_EM + 0.001:
                                    cluster_parts.append(
                                        "\t" if gap > size * COLUMN_GAP_EM else " "
                                    )
                            cluster_parts.append(text)
                        span["text"] = "".join(cluster_parts)
                        span["metrics"] = {
                            "font_xref": span["xref"],
                            "x": span["x"],
                            "y": span["y"],
                            "total_glyphs": decoded.total_glyphs,
                            "unknown_count": decoded.unknown_count,
                            **asdict(
                                verify_roundtrip(
                                    decoded.text, span["gids"], mapping, _budget=budget
                                )
                            ),
                        }
            font_reports.append(
                {
                    **asdict(candidate),
                    "font_xrefs": sorted(font_xrefs),
                    "pages": sorted({p for f in group for p in f.pages}),
                    "font_hash": mapping.font_hash,
                    "map_entries": len(mapping.mapping),
                    "estimated_map_bytes": mapping.estimated_bytes,
                    "build_seconds": mapping.build_seconds,
                    "on_demand_seconds": mapping.on_demand_seconds,
                    "collisions": mapping.collisions,
                    "cmap_repairs": mapping.cmap_repairs,
                    "skipped": mapping.skipped,
                    "gsub_features": mapping.gsub_features,
                }
            )
            del mapping
    if inferred:
        warnings.append(
            {
                "code": "inferred-cmap",
                "message": "Output uses inferred cmap mappings; round-trip agreement does not establish correctness.",
                "codepoints": sorted(inferred),
            }
        )
    texts = []
    reports = []
    recovered_pages = []
    for number, width, height, lines in page_data:
        blocks = []
        runs = []
        for line in lines:
            parts: list[str] = []
            previous = None
            for span in line:
                text = span["text"]
                if (
                    previous is not None
                    and parts
                    and not parts[-1].endswith(" ")
                    and not text.startswith(" ")
                ):
                    gap = span["x"] - previous["end"]
                    if gap > span["size"] * WORD_GAP_EM + 0.001:
                        parts.append("\t" if gap > span["size"] * COLUMN_GAP_EM else " ")
                parts.append(text)
                previous = span
                if "metrics" in span:
                    runs.append(span["metrics"])
            text = "".join(parts).rstrip()
            blocks.append(
                TextBlock(
                    raw_text=text,
                    unicode_text=text,
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
        page = PDFPage(number, blocks, width, height)
        recovered_pages.append(page)
        texts.append(page.unicode_text)
        metrics = {
            key: sum(r[key] for r in runs)
            for key in ("total_glyphs", "unknown_count", "matching_glyphs", "compared_glyphs")
        }
        ratio, issues = _well_formedness(page.unicode_text)
        complete = all(r["verification_complete"] for r in runs)
        reports.append(
            {
                "page": number,
                **metrics,
                "runs": runs,
                "verification_complete": complete,
                "roundtrip_match_ratio": (
                    metrics["matching_glyphs"] / metrics["compared_glyphs"]
                    if metrics["compared_glyphs"]
                    else 1
                )
                if complete
                else None,
                "well_formedness_ratio": ratio,
                "well_formedness_issues": issues,
            }
        )
    text = "\n\f\n".join(texts) + "\n"
    total = sum(p["total_glyphs"] for p in reports)
    unknown = sum(p["unknown_count"] for p in reports)
    compared = sum(p["compared_glyphs"] for p in reports)
    complete = all(p["verification_complete"] for p in reports)
    if not complete:
        warnings.append(
            {
                "code": "verification-incomplete",
                "message": "Round-trip diagnostic budget exhausted; recovered text is retained, but verification is incomplete.",
            }
        )
    report = {
        "source": str(pdf_path),
        "script": "telugu",
        "verification_note": VERIFICATION_NOTE,
        "total_glyphs": total,
        "unknown_count": unknown,
        "glyph_coverage": 1 - unknown / total if total else 1,
        "verification_complete": complete,
        "alignment_work": budget.alignment_work,
        "roundtrip_match_ratio": (
            sum(p["matching_glyphs"] for p in reports) / compared if compared else 1
        )
        if complete
        else None,
        "well_formedness_ratio": _well_formedness(text)[0],
        "collisions": sum(len(f["collisions"]) for f in font_reports),
        "inferred_cmap_entries": sum(
            sum(r["source"].startswith("inferred") for r in f["cmap_repairs"]) for f in font_reports
        ),
        "map_build_seconds": sum(
            f["build_seconds"] for f in {f["font_hash"]: f for f in font_reports}.values()
        ),
        "fonts": font_reports,
        "pages": reports,
        "warnings": warnings,
        "spacing_ambiguity": [w for w in warnings if w["code"] == "spacing_ambiguity"],
        "inferred_codepoints": sorted(inferred),
        "limitations": [
            "Round-trip is internal consistency, not semantic verification.",
            "Invisible (Tr 3) and clipping-only (Tr 7) text is excluded.",
            "ZWJ/ZWNJ cannot be distinguished when their glyphs and advances coincide.",
            "Reading order uses approximate horizontal baseline rows.",
        ],
    }
    return RecoveryResult(text, report, recovered_pages, warnings, sorted(inferred))


def recover_document(
    document: PDFDocument, page_numbers: set[int], *, strict_cmap: bool = False
) -> PDFDocument:
    """Recover selected pages as positioned blocks; retain all other pages.

    Blocks follow the same approximate visual lines as ``recover_pdf``. Each
    uses the union of run bounds and the leading run's font name/size. Both text
    fields contain recovered Unicode because raw glyph codes are not Unicode.
    """
    if not page_numbers:
        return document
    result = recover_pdf(
        document.filepath, page_numbers=page_numbers, strict_cmap=strict_cmap, convert_legacy=True
    )
    pages = {page.page_number: page for page in result.pages}
    return replace(
        document,
        pages=[pages.get(page.page_number, page) for page in document.pages],
        warnings=result.warnings,
        inferred_codepoints=result.inferred_codepoints,
    )
