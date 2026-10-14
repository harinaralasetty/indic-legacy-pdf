# Recovering Telugu text from glyph-ID PDFs

Some PDFs embed a normal Unicode font but omit `/ToUnicode`. Copying their text
then exposes font glyph IDs rather than Unicode. This is different from the
legacy ASCII encodings handled by `UnicodeConverter`; a character replacement
mapping cannot undo Indic shaping.

## Usage

Install the `glyph` extra (`uharfbuzz` and `fonttools`). It is also included in
`dev`. Detection requires fontTools to verify Telugu support in the embedded
font program; without it, detection returns no glyph-ID candidates and preserves
the existing detection path. HarfBuzz is required for recovery only. PDF output does not import HarfBuzz. Recovery
gives an installation message when either dependency is missing.

```bash
uv sync --extra glyph
uv run legacylipi detect INPUT.pdf
uv run legacylipi translate INPUT.pdf --translator mock --format markdown -o TRANSLATED.md
uv run legacylipi translate INPUT.pdf --translator mock --format pdf --structure-preserving -o TRANSLATED.pdf
uv run legacylipi recover-text INPUT.pdf -o OUT.txt --report REPORT.json
```

Without `--report`, the report is written to `OUT.report.json`. Input, text and
report paths must differ, including symlinks and hard links. Existing PDF outputs
are refused regardless of extension. Each text/report file is written through a
temporary file in the same directory and atomically replaced; the pair is not a
single transaction. Missing candidate fonts produce a nonzero exit status.
A completed recovery can still contain unknown glyphs: review the metrics.

## Translation and positioned output

CLI `translate`, `convert`, `extract`, and the API translation pipeline share
`UnicodeConverter.convert_with_recovery`. When detection returns `glyph-id`, it
recovers candidate font runs on pages whose per-page encoding equals `glyph-id`.
Legacy runs on those same pages are converted by their own font/text detection,
then combined with recovered Telugu in reading order. Other pages retain the
existing conversion handling. Explicit `USER_OVERRIDE` results in `page_encodings`
(and CLI `--encoding`) take precedence over automatic font matches. Translation defaults to source language `te`;
CLI `--source-lang` and API `source_lang` override that default.

The commands above use `mock`, which echoes text with a prefix without a network
request. Select a configured translation backend for actual translation.
Text, Markdown (including bilingual source text), and positioned PDF output all
receive recovered Unicode. PDF output uses the same font selection and TextWriter
as ordinary documents. It does not shape Indic scripts. If the written output
contains Telugu (including source text or Telugu translations), the CLI emits a
warning and API results include `pdf-indic-unshaped` in their warning list. Use
text/markdown output and a viewer with Indic shaping for Telugu. English-only
PDF output needs no warning. Optional recovery dependencies are not required
for ordinary Unicode or Latin PDF output.

`recover_document` reuses `recover_pdf`'s run decoding and visual-line grouping.
It creates one `TextBlock` per approximate baseline row, with the union of its
run bounding boxes and the leading run's font name and size. Both `raw_text` and
`unicode_text` contain recovered Unicode. Mixed font sizes within a line and
semantic column/table reconstruction are not preserved. Document metadata,
fonts, page numbers, and unselected pages are retained. The API's
`structure_preserving` mode and CLI `--structure-preserving --format pdf`
translate and place these blocks individually.

Automatic translation does not save a recovery report; use `recover-text` to
inspect unknown glyphs, ambiguities, and inferred cmap entries. Missing recovery
dependencies raise the same `legacylipi[glyph]` installation message. Without
fontTools, automatic detection cannot verify glyph-ID candidates; install the
extra before relying on detection, or force `--encoding glyph-id` in the CLI to
request recovery explicitly.

## How it works

1. Detect embedded Type0 Identity-H/V fonts without `/ToUnicode`, by PDF object
   references rather than font names. Verify Telugu support with fontTools:
   GSUB/GPOS script tags `tel2` or `telu`, Unicode cmap entries in U+0C00–U+0C7F,
   or glyph names encoding those codepoints or ending in `telu` (including
   dotted variants). Latin-only fonts do not qualify. Report only xrefs and
   1-indexed pages with nonempty text operands using those fonts, including Forms.
2. Read raw two-byte big-endian text operands by font resource, including hex and
   literal strings, `Tj`, `TJ`, quote operators and Form XObjects. Apply an explicit
   `/CIDToGIDMap` when present; Identity encoding maps character codes to CIDs,
   **not necessarily directly to GIDs**.
3. Independently check those GIDs against MuPDF `get_texttrace()` GIDs, then use
   its character positions. In the supplied samples, `rawdict` also exposed
   `chr(GID)`, but that representation is not assumed by the decoder.
4. Shape supported Telugu clusters with HarfBuzz. Enumerate independent vowels,
   consonants, every two-consonant conjunct, signs, modifiers and terminal
   viramas. Three-consonant conjuncts include all supported vowel signs; their
   modifiers are decoded as suffixes. Four-to-six-consonant clusters are searched
   on demand when they extend an already matched cluster; each extension must
   reshape to a longer exact GID prefix. Also include available Telugu digits,
   ASCII, punctuation and danda. Unsupported codepoints and reasons are recorded.
5. Decode by greedy longest GID tuple match. An unknown GID produces one U+FFFD.
   Collisions retain every enumerated alternative; the canonical choice prefers
   shortest strings, then NFC, then Unicode lexical order. This is deterministic,
   not a linguistic ranking.
6. Reshape each complete recovered font run and compare with its original GIDs.
   This uses the same shaping and cmap assumptions that built the reverse map;
   it checks internal consistency, not the correctness of inferred cmap entries.

Glyph-ID discovery inspects only used font resources. A legacy run does not
exclude glyph recovery on the same page. Documents without glyph candidates
retain the original detector/converter behavior, including document-font order,
unused legacy resource precedence, and page-wide encoding decisions. Those
historical rules can convert ordinary text under a legacy encoding; they are
preserved for compatibility and covered by frozen main-pipeline golden outputs.
No additional non-glyph behavior correction is introduced here.

Font verification verdicts are cached by xref within a document scan and by
original font SHA-256 across scans (up to 128 hash/boolean entries in memory).
Unused resources are not decoded and consume no glyph-font budget. Undecodable
or unsupported used font programs are excluded from automatic detection; explicit
recovery returns a controlled error naming the font. Unsupported content streams
or ambiguous resource associations are conservatively excluded from detection.

Maps are cached only in memory, keyed by the original font SHA-256 and script,
with a one-map limit. Concurrent cold requests share the same object under a lock;
different-key builds also serialize to bound simultaneous cache construction. No font or map files are written. Build timings include
cmap repair and enumeration. Three-consonant enumeration took under 20 seconds
per font in local tests; exact measured times are in the JSON reports. The
script parameter currently accepts only `telugu`; unsupported scripts fail
explicitly. The `recover-text` command itself does not translate.

## Trimmed cmap repair and assumptions

HarfBuzz needs Unicode-to-glyph mappings even when all glyph outlines and GSUB
rules survive. Repairs modify an **in-memory copy**, leaving the embedded font
and GID order untouched.

First use semantic Unicode/Adobe glyph names or recognized Telugu vowel-sign
names. Do not use alternate glyph names as base characters. Some producers also
remove all `post` names, as in the supplied Gautami fonts. For these fonts, a
fallback infers small gaps between existing cmap anchors when:

- GSUB exists, `post` is format 3, and at least 20 known Telugu vowel/consonant
  anchors are strictly increasing in GID order;
- the intervening assigned Unicode codepoint count exactly fits the GID gap,
  using the current Unicode repertoire or, for older fonts, Unicode 3.2;
- the gap has at most 12 assigned codepoints and no GID already mapped elsewhere.

Each such repair is explicitly labelled **inferred Unicode-ordered cmap gap**,
with its anchors and assumed repertoire. This is a documented heuristic, not
proof of the missing cmap's contents. It was tested by deleting cmap entries and
post names from a known ground-truth Noto font. Missing entries that cannot be
repaired stay in `skipped`. Visible reordered anchors prevent interpolation. Hidden swaps inside a missing
gap cannot be detected: a synthetic swapped U+0C25/U+0C26 case gives incorrect
text with a perfect round trip. This is why uncertainty is propagated.

## Reading the report

- `total_glyphs` and `unknown_count`: glyph-ID candidate fonts only. Ordinary
  fonts retain MuPDF's Unicode extraction and are excluded from glyph metrics.
- `glyph_coverage`: `1 - unknown_count / total_glyphs`.
- `roundtrip_match_ratio`: matching aligned GIDs divided by the larger of the
  original and reshaped run lengths, aggregated by glyph counts. Both insertions
  and deletions reduce it. Page/run records include mismatching ranges and GIDs.
  Inserted layout tabs, spaces and page breaks do not enter this calculation.
  Matching `.notdef` boxes are excluded from successful matches. The value is
  `null` when diagnostic verification is incomplete, rather than a misleading
  partial ratio.
- `verification_complete`: present on run, page, and document records. `false`
  means at least one unequal run was not aligned within the diagnostic allowance.
  Recovery still returns text, coverage, and a `verification-incomplete` warning.
  `alignment_work` records the consumed document alignment allowance.
- `verification_note`: explains that round-trip = internal consistency of
  reading order/whitespace/ambiguity; it does NOT verify that inferred cmap
  mappings are correct. Manual comparison was done once on one page region by a
  reviewer, not reproducible from the repository; the repository evidence is
  synthetic ground-truth tests.
  This run does not perform a visual check. Consistency is checked within runs;
  inserted layout whitespace and reading order between runs are not checked.
- `well_formedness_ratio`: fraction of whitespace-delimited output words without
  U+FFFD, an initial combining mark, or terminal virama. Terminal virama can be
  legitimate in loanwords and abbreviations; flagged words are review candidates,
  not proven spelling errors. This metric includes ordinary-font output too.
- `fonts`: original font hashes, cmap repairs, skipped codepoints, GSUB feature
  tags, map sizes, build times and collision alternatives.
- `pages`: all pages, including pages without candidate text. Empty comparisons
  are reported as 1 with zero glyph counts and must not be mistaken for evidence.

A high round-trip score is **self-consistent by construction** for mapped
fragments: the map was built by shaping, so decoding and reshaping reuse the
same cmap assumptions. Complete-run comparison can expose order, whitespace,
or ambiguity mismatches within runs, but cannot validate inferred cmap mappings,
original Unicode, linguistic correctness, or identity accuracy. It cannot
distinguish different Unicode strings that shape identically. Inferred entries
are listed in `fonts[].cmap_repairs`; manual comparison was done once on one page
region by a reviewer, not reproducible from the repository. The repository
evidence is synthetic ground-truth tests. Page scores can be lower than the
overall score.

## Limits

- Reading order is approximate horizontal baseline rows, then left-to-right.
  Wide gaps become tabs and pages are separated by form feeds. Complex columns,
  rotated/vertical text, overlapping annotations and semantic tables need review.
  Identity-V fonts are detected, but vertical shaping/layout is not validated.
- Rare clusters must extend a matching enumerated prefix for the bounded
  on-demand search (up to six consonants) to find them. Longer clusters,
  joiner-dependent forms and unusual repeated/noncanonical marks may decode
  compositionally or remain unknown; there is no unrestricted search fallback.
- Inline images in content streams are rejected explicitly (image XObjects are
  fine). Ambiguous candidate/ordinary font names, unsupported resource layouts,
  or a raw/trace GID discrepancy fail explicitly rather than silently misassigning
  glyphs. Invisible render mode 3 and clipping-only mode 7 are excluded. Modes
  0/1/2/4/5/6 recover visible paint, deduplicating fill+stroke traces in 2/6.
  Geometry clipping/occlusion is not evaluated; painted but occluded text may remain.
- Encrypted PDFs requiring passwords and non-sfnt embedded CFF fonts are not
  supported. Shaping-engine differences from the producer may lower the score.
- Candidate page lists require nonempty text operands, not just resource
  presence. Modes 3/7 are excluded, but this does not establish geometric visibility.
- Telugu verification establishes font support, not the language of every text
  run in a multilingual font. Fonts stripped of all script/cmap/name evidence
  cannot be verified and are excluded.

## Tests and private data

Synthetic PDFs are generated during tests from the OFL Noto Sans Telugu fixture
and a tiny Latin-only TrueType font built offline with fontTools FontBuilder;
the glyph fixtures contain no private PDFs or proprietary font bytes. The older
`tests/data/input.pdf` is a real government document predating this branch; this
repository and its history therefore cannot be described as entirely synthetic.

```bash
uv run --offline pytest tests/test_glyph_recovery.py tests/test_optional_deps.py -q
LEGACYLIPI_TELUGU_SAMPLES=/absolute/path/to/pdfs \
  uv run --offline pytest tests/test_glyph_recovery.py -q -k real_samples
```

The opt-in smoke test requires less than 1% unknown glyphs and at least 99%
round-trip match **per document**. Page metrics are retained for inspection.
Keep recovered private text and reports outside the repository.

## Warnings, strict mode and budgets

`recover_pdf`, `RecoveryResult`, recovered `PDFDocument`, API `PipelineResult`,
session results and completion events expose structured `warnings` and
`inferred_codepoints` (only inferred codepoints actually used in output).
All four CLI commands (`recover-text`, `translate`, `convert`, `extract`) print
these warnings, including in quiet mode. `--strict-cmap` / `strict_cmap=True`
(default false) refuses recovery if any output mapping needs inference.
Semantic glyph-name repairs do not count as inferred cmap gaps.

Word boundaries use residual horizontal gaps at decoded cluster boundaries,
regardless of `TJ`, `Tj`, `Td`, or `Tm` syntax. Expected advances include CID
widths and `Tc`, transformed by font size, `Tz`, the text matrix and graphics
matrix. `Ts` and `TL` are reflected in traced baselines. PDF `Tw` only applies
to single-byte character code 32, so it does not add space to two-byte Identity
codes, even if a code maps to a Unicode space. A residual gap greater than
0.15 em adds a space; above 2 em it adds a tab. Gaps from 0.10 through 0.25 em
are explicitly uncertain: `spacing_ambiguity` entries appear in both the report
field and the CLI/API warning list. Encoded spaces are preserved. Whitespace
inferred from layout is heuristic and is not verified by the round-trip score.

A space GID after virama with zero advance may be a ZWJ/ZWNJ alias. Recovery
keeps a joiner-less form and adds `ambiguous-joiner`; a positive observed advance
preserves the word space. Direct GID-only decoding lacks positions and takes
the conservative joiner-less choice. Neither choice proves original spelling.
There is no `--include-invisible` option.

Default hard recovery budgets (controlled `ValueError`, usually `RecoveryBudgetError`):

| Resource | Limit |
|---|---:|
| Content tokens/operators, aggregate per traversal | 2,000,000 × page count, capped at 20,000,000 |
| Form/page expansions per traversal | 4,096 |
| Form nesting | 32 |
| Decoded bytes per stream | 16 MiB |
| Total unique decoded stream bytes per traversal | 64 MiB |
| Distinct used candidate font programs per document, by SHA-256 | 8 |
| Accounted map allocation per build | 768 MiB |
| Retained global maps | 1 |

Diagnostic budgets are separate from the hard recovery limits:

| Diagnostic work | Limit |
|---|---:|
| Unequal alignment length product per run | 250,000 |
| Aggregate unequal alignment length products | 4,000,000 |

Exhausting either diagnostic limit retains recovered text and reports
`verification_complete: false`, a `null` round-trip ratio, and a warning. Hard
content/font/map limits still stop recovery. `--strict-cmap` controls inferred
mappings only; it does not turn diagnostic exhaustion into a recovery failure.

Discovery memoizes Form/resource contexts; recovery counts actual expansions
and refuses exponential paint graphs before MuPDF tracing. Unfiltered, Flate,
ASCIIHex, ASCII85, and LZW streams have bounded decoding; supported filter chains
use MuPDF streaming decoders. Predictor transforms and DecodeParms arrays remain
unsupported. Duplicate font bytes share storage and one map by SHA-256, so
per-page copies do not consume additional font allowances. Maps are processed
one distinct program at a time and released before the next build. The
map counter estimates Python allocations conservatively; it is not an OS RSS cap,
and callers retaining public map objects own their lifetime. Font/native parser
allocations are not a process-isolation guarantee. Identical glyph sequences
use a linear fast path; unequal alignment is bounded before SequenceMatcher.

Positioned translation fails with a nonzero CLI status when at least 50% of
nonempty blocks fail. Below that threshold failed blocks retain source text,
carry `translation_error`, and produce visible CLI/API warnings.

`PipelineResult` is a two-field NamedTuple subclass: indexing, `len(result) == 2`,
and two-value unpacking remain compatible, with warnings attached separately.
The review tests use frozen golden outputs and generated fixtures, not a local
`main` ref. Large generated-document checks are marked `slow` and skipped when
`CI` is set; unit complexity checks use deterministic operation/expansion counts.

On discovery budget exhaustion, a resource-only check for an embedded,
Telugu-verified Identity-H/V font without ToUnicode propagates the controlled
budget error if present; otherwise ordinary detection continues. Explicit
recovery still returns the budget error. Malformed CID `DW`, `W`, `DescendantFonts` and
`CIDToGIDMap` structures are rejected with descriptive errors.
