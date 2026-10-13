# Priyaanka F7 evaluation

Copied from `telugu-m0/proto/F7/F7_report.md`; these are recorded research results, not a new evaluation.

F6 emissions and all inherited rules are unchanged. Split: 934 TRAIN / 215 VALIDATION / 287 TEST, seed 20261013. Candidate selection reads only the saved validation predictions; the historical TEST records are loaded after the decision hash freeze. Historical TEST examples were supplied in the task: this is not a fresh blind test. No word-specific patches. Validation reuse makes its gains selection-biased.

## Validation deltas

Validation exact: 196/215 → 198/215; edits 35 → 27. Validation character accuracy: 97.16% → 97.81%. Admission requires a positive exact delta and no loss of any currently-correct word. Ordered deployed rules: `[{"id": "R1", "variant": "whole"}]`.

## TEST (single final evaluation)

| Model | Exact | Exact % | Char % | Edits |
|---|---:|---:|---:|---:|
| F2 | 205/287 | 71.43 | 92.06 | 132 |
| F4 | 223/287 | 77.70 | 93.56 | 107 |
| F5 | 223/287 | 77.70 | 92.96 | 117 |
| F6 | 231/287 | 80.49 | 93.86 | 102 |
| F7 | 255/287 | 88.85 | 96.87 | 52 |
| F7_without_R1 | 231/287 | 80.49 | 93.86 | 102 |
| F7_without_R2 | 255/287 | 88.85 | 96.87 | 52 |
| F7_without_R3 | 255/287 | 88.85 | 96.87 | 52 |
| F7_without_R4 | 255/287 | 88.85 | 96.87 | 52 |
| F7_without_R5 | 255/287 | 88.85 | 96.87 | 52 |

Char accuracy = 1 − codepoint Levenshtein edits / reference codepoints; exact includes punctuation. Each removal ablation deletes only that F7 rule, without refitting; removing rejected rules is an identity ablation. F2/F4 used 1,149 training words versus 934 for F5/F6.

## Unlabeled well-formedness

| Sample | Words | F6 valid | F7 valid | Unknown occurrences | Unknown names |
|---|---:|---:|---:|---:|---:|
| F6_original_300 | 300 | 241 (80.33%) | 270 (90.00%) | 0 | 0 |
| other_PDFs_300 | 300 | 106 (35.33%) | 123 (41.00%) | 145 | 22 |
| uecf_s2009.pdf | 103 | 12 (11.65%) | 13 (12.62%) | 50 | 6 |
| loksatta_2010.pdf | 115 | 90 (78.26%) | 106 (92.17%) | 1 | 1 |
| ou_pg_telugu.pdf | 82 | 4 (4.88%) | 4 (4.88%) | 94 | 18 |

The other-PDF sample contains 300 uniformly random word occurrences pooled across the three named PDFs, seed 20261013, regular and Bold. Per-PDF rows partition that sample; unknown names were not excluded. The original 300-word sample required known glyph names. The final OU sample splits actual space glyphs into word boundaries; this extraction correction did not change the decoder, validation selection or TEST evaluation.

## Remaining limits

Well-formedness reuses F6 structural checks: no orphan/duplicate dependent sign, no sign before virama, no orphan or invalid-successor virama, no U+FFFD/UNKNOWN. Bare terminal consonant+virama and explicit joiners are permitted. This is a conservative heuristic, not a complete Telugu grammar; it can reject valid non-Telugu tokens and cannot establish word accuracy.

The TEST results still contain ordering, punctuation, sign and consonant errors. Evaluation labels and TEST words come from one public Telangana SCERT Telugu textbook. Unknown glyph names and other font-build differences limit transfer even within Priyaanka. There is no independent fluent-reader validation or OCR cross-check. This prototype is not safe to rely on for anything important.
