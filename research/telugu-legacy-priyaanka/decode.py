#!/usr/bin/env python3
"""Standalone Priyaanka F7 decoder. Standard library only; no PDF/font dependency.

Input is a word's glyph names in PyMuPDF content-stream order, not Unicode
characters or GIDs. Keep font and whitespace boundaries outside this function.
CLI: python decode.py mapping_priyaanka.json name1 name2 ...
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

from repair import repair

SIGNS = frozenset("ాిీుూృౄెేైొోౌౢౣ")
VIRAMA = "్"


def is_base(text):
    return any("క" <= c <= "హ" for c in text) and not text.startswith(VIRAMA)


def is_sign(text):
    return bool(text) and all(c in SIGNS for c in text)


def load(path):
    return json.loads(Path(path).read_text())


def emit(symbols, model):
    """Longest accepted unit first, then per-glyph emission; stable tie order."""
    units = model.get("_unit_index")
    if units is None:
        units = {}
        for unit in model.get("multi_glyph_units", []):
            units.setdefault(unit["glyphs"][0], []).append(unit)
        for values in units.values():
            values.sort(key=lambda u: (-len(u["glyphs"]), -u["support"], u["glyphs"], u["unicode"]))
        model["_unit_index"] = units
    chunks = []
    i = 0
    while i < len(symbols):
        unit = next(
            (
                u
                for u in units.get(symbols[i], [])
                if tuple(symbols[i : i + len(u["glyphs"])]) == tuple(u["glyphs"])
            ),
            None,
        )
        n = len(unit["glyphs"]) if unit else 1
        chunks.append(
            {
                "glyphs": tuple(symbols[i : i + n]),
                "text": unit["unicode"] if unit else model["table"].get(symbols[i], "\ufffd"),
                "start": i,
                "end": i + n,
            }
        )
        i += n
    return chunks


def chunk_class(text):
    if not text:
        return "empty"
    if is_sign(text):
        return "sign"
    if is_base(text):
        return "base"
    if text.startswith(VIRAMA):
        return "tail"
    return "boundary"


def context_matches(chunks, i, context):
    for key, value in context.items():
        side, kind = key.split("_", 1)
        j = i + (-1 if side == "left" else 1)
        actual = (
            ("BOS" if j < 0 else "EOS")
            if not 0 <= j < len(chunks)
            else (list(chunks[j]["glyphs"]) if kind == "glyphs" else chunk_class(chunks[j]["text"]))
        )
        if actual != value:
            return False
    return True


def logical_order(chunks, rules):
    """Attach detached marks to a base without crossing punctuation boundaries.

    A base plus following conjunct tails is emitted before its attached signs.
    Unattached marks remain visible; nothing is silently discarded.
    """
    emitted = [c["text"] for c in chunks]
    attached = {}
    removed = set()
    for i, c in enumerate(chunks):
        if not is_sign(c["text"]):
            continue
        rule = next(
            (
                r
                for r in sorted(rules, key=lambda r: -len(r.get("context", {})))
                if tuple(r["glyphs"]) == c["glyphs"]
                and r["unicode"] == c["text"]
                and context_matches(chunks, i, r.get("context", {}))
            ),
            None,
        )
        if rule is None or rule["direction"] == "none":
            continue
        step = -1 if rule["direction"] == "previous" else 1
        target = None
        j = i + step
        while 0 <= j < len(chunks):
            value = emitted[j]
            if is_base(value):
                target = j
                break
            if value and not (
                is_sign(value)
                or value.startswith(VIRAMA)
                or all(unicodedata.category(x).startswith("M") for x in value)
            ):
                break
            j += step
        if target is not None:
            attached.setdefault(target, []).append((i, c["text"]))
            removed.add(i)
    result = []
    consumed = set(removed)
    for i, value in enumerate(emitted):
        if i in consumed:
            continue
        result.append(value)
        if i not in attached:
            continue
        # Post-base conjunct tails belong before the vowel, even if the visual
        # stream puts a vowel or an empty head between base and subscript.
        j = i + 1
        while j < len(chunks):
            following = emitted[j]
            if j in removed or not following:
                j += 1
                continue
            if following.startswith(VIRAMA):
                result.append(following)
                consumed.add(j)
                j += 1
            else:
                break
        result.extend(t for _, t in sorted(attached[i]))
    return "".join(result)


def decode(symbols, model):
    text = logical_order(emit(symbols, model), model.get("ordering_rules", []))
    for rule in model.get("normalizations", []):
        if rule["kind"] == "nfc":
            text = unicodedata.normalize("NFC", text)
        elif rule["kind"] == "replace":
            text = text.replace(rule["source"], rule["target"])
        else:
            raise ValueError("Unknown normalization: " + rule["kind"])
    return repair(text, model.get("repair_rules", []))


def decode_words(words, model):
    """One list of glyph names per word, preserving word boundaries."""
    return " ".join(decode(word, model) for word in words)


def decode_lines(lines, model):
    """Lines -> words -> glyph names; preserve line and word boundaries."""
    return "\n".join(decode_words(line, model) for line in lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mapping")
    ap.add_argument("glyphs", nargs="*")
    ap.add_argument("--json-input", help="JSON file, or - for stdin")
    ap.add_argument("--level", choices=["word", "words", "lines"], default="word")
    args = ap.parse_args()
    model = load(args.mapping)
    data = (
        json.loads(
            sys.stdin.read() if args.json_input == "-" else Path(args.json_input).read_text()
        )
        if args.json_input
        else args.glyphs
    )
    print({"word": decode, "words": decode_words, "lines": decode_lines}[args.level](data, model))
