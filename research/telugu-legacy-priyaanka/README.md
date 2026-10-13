# Priyaanka Telugu legacy-font research

This research prototype converts text extracted from PDFs set in the legacy
Telugu font family **Priyaanka** to Unicode. These PDFs embed Type1/CFF subsets
with standard Latin glyph **names**, such as `exclam` and `udieresis`. The decoder
uses a per-glyph-name table, learned multi-glyph units, and a few reordering and
repair rules. It takes glyph names in PDF content-stream order, not extracted
Latin characters or glyph IDs.

The maintainer read approximately 1,500 word images and labelled approximately
90 glyph names by reading highlighted examples; the rest was learned by
alignment. The labelling and evaluation words, including TEST words, come from
**one document**: a public Telangana SCERT Telugu textbook PDF
(`scert_5telugu_2020.pdf`). The copied artifacts are from
`telugu-m0/proto/F7/`, `F3/labels_claude.json`, and
`F1_batches/{transcripts_claude,words}.json`.

Held-out TEST: **255/287 words exactly right (88.85%)**, with **96.87% character
accuracy**. This is a historical held-out split, not a fresh blind test;
validation reuse makes its gains selection-biased. See [evaluation.md](evaluation.md).

On other PDFs using the same font family, only a minority of the pooled sample
decoded to structurally well-formed Telugu: **123/300 (41.00%)**. Missing glyph
names and other font-build differences limit transfer. Exact recorded figures:

| Sample | Words | F6 valid | F7 valid | Unknown occurrences | Unknown names |
|---|---:|---:|---:|---:|---:|
| F6_original_300 | 300 | 241 (80.33%) | 270 (90.00%) | 0 | 0 |
| other_PDFs_300 | 300 | 106 (35.33%) | 123 (41.00%) | 145 | 22 |
| uecf_s2009.pdf | 103 | 12 (11.65%) | 13 (12.62%) | 50 | 6 |
| loksatta_2010.pdf | 115 | 90 (78.26%) | 106 (92.17%) | 1 | 1 |
| ou_pg_telugu.pdf | 82 | 4 (4.88%) | 4 (4.88%) | 94 | 18 |

The three per-PDF rows partition the pooled 300 words. Well-formedness is a
structural heuristic, not word accuracy.

## Limits

This covers **one font family**. It has not been validated by a fluent Telugu
reader beyond the maintainer's own reading, and there is no OCR cross-check.
It is not connected to `legacylipi translate` or any other pipeline command.
It is **not safe to rely on for anything important**. Unknown glyph names emit
the visible replacement character `�` (U+FFFD). Some mapped names intentionally
emit an empty string; the hand-labelled empty pieces are explained in `labels.json`.

## Usage

From this folder, use Python and the standard library only:

```sh
printf '%s\n' '[["braceright", "endash"], ["quoteleft", "radical", "oslash", "sterling"]]' > /tmp/priyaanka-words.json
python decode.py mapping_priyaanka.json --json-input /tmp/priyaanka-words.json --level words
```

Output: `ఊగి తోక`. Each inner list is one word; keep font and word boundaries
separate. `word_symbols.json` holds glyph-name sequences by word ID, and
`word_readings.json` holds only non-empty `W` readings not starting with `~`.

To inspect glyph names with the repository's existing PyMuPDF dependency, this
10-line example requires a page with exactly one embedded font. It uses trace
glyph IDs and the embedded font's names, through PyMuPDF's low-level MuPDF
binding, rather than guessing names from Unicode. It prints one list per span;
spans are not necessarily words. See [PyMuPDF text traces](https://pymupdf.readthedocs.io/en/latest/functions.html#Page.get_texttrace).

```python
import json
import pymupdf
with pymupdf.open("input.pdf") as doc:
    page = doc[0]
    fonts = page.get_fonts()
    assert len(fonts) == 1, "Example requires one font on this page"
    font = pymupdf.Font(fontbuffer=doc.extract_font(fonts[0][0])[3])
    for span in page.get_texttrace():
        names = [pymupdf.mupdf.fz_get_glyph_name2(font.this, c[1]) for c in span["chars"]]
        print(json.dumps(names))
```

This snippet was run with PyMuPDF 1.26.7 on a generated PDF containing `!ü A`
in an embedded font, returning `exclam`, `udieresis`, `space`, `A`. No private
PDF was used. This checks extraction mechanics, not Priyaanka decoding. For
real PDFs, resolve each exact font subset and preserve word boundaries; traces
do not insert spaces where the PDF omits them.

## Licensing and data

This folder contains no font programs and no PDF content beyond word-level
readings and glyph-name sequences. The source textbook and fonts retain their
own licensing; their redistribution is not implied by this research folder.
