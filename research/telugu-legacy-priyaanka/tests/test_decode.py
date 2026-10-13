import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decode import decode, load  # noqa: E402
from repair import R1  # noqa: E402


@pytest.fixture
def model():
    return load(ROOT / "mapping_priyaanka.json")


@pytest.mark.parametrize("word_id", ["W0001", "W0006", "W0011", "W0012", "W0013"])
def test_known_short_word(word_id, model):
    symbols = load(ROOT / "word_symbols.json")[word_id]["symbols"]
    reading = load(ROOT / "word_readings.json")[word_id]
    assert decode(symbols, model) == reading


def test_labelled_glyphs_are_in_mapping(model):
    labels = load(ROOT / "labels.json")
    assert labels.keys() <= model["table"].keys()
    for label in labels.values():
        if label["unicode"] == "":
            assert label["note"]


def test_r1_moves_vowel_after_virama_chain():
    assert R1("నువు్వ") == "నువ్వు"


def test_unknown_glyph_is_explicit(model):
    assert decode(["__unknown_glyph__"], model) == "\ufffd"


def test_json_shapes():
    data = {
        name: json.loads((ROOT / f"{name}.json").read_text())
        for name in ("mapping_priyaanka", "labels", "word_readings", "word_symbols")
    }
    assert all(isinstance(value, dict) and value for value in data.values())
    mapping = data["mapping_priyaanka"]
    assert mapping["font_family"] == "Priyaanka"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in mapping["table"].items())
    for field in ("multi_glyph_units", "ordering_rules", "normalizations", "repair_rules"):
        assert isinstance(mapping[field], list)
        assert all(isinstance(rule, dict) for rule in mapping[field])
    assert all(
        isinstance(name, str)
        and isinstance(label, dict)
        and isinstance(label["unicode"], str)
        and isinstance(label["note"], str)
        for name, label in data["labels"].items()
    )
    assert all(
        name.startswith("W") and isinstance(value, str) and value and not value.startswith("~")
        for name, value in data["word_readings"].items()
    )
    assert data["word_readings"].keys() <= data["word_symbols"].keys()
    assert all(
        isinstance(name, str)
        and isinstance(word, dict)
        and set(word) == {"symbols"}
        and isinstance(word["symbols"], list)
        and all(isinstance(symbol, str) for symbol in word["symbols"])
        for name, word in data["word_symbols"].items()
    )
