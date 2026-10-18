# Filing, checking, and reading

The [workflow](workflow.md) owns evidence and source selection. These commands
reproduce explicit choices: `file` publishes, `check` verifies, and `reading`
selects notes/preferred text. No command identifies documents or expands scope.

## Filing assignments

Save a bounded UTF-8 JSON recipe outside document folders. `source_root` and
`text_root` resolve relative to the recipe; `document` resolves below `--output`.
Replace the fictional hash placeholders with recorded SHA-256 values:

```json
{
  "schema": 1,
  "source_root": "../originals",
  "text_root": "../organized/Needs review/Selected range",
  "assignments": [
    {
      "source": "Box 2/IMG_1001.jpg",
      "source_sha256": "<64 lowercase hexadecimal characters>",
      "document": "Network planning notes - Provisional packet",
      "description": "Network planning notes",
      "disposition": "review",
      "capture_order": 1,
      "preferred_text": {
        "path": "pages/by-source/Box 2/IMG_1001.jpg/extract.txt",
        "sha256": "<64 lowercase hexadecimal characters>"
      }
    }
  ]
}
```

Each selected original needs one primary assignment: `document`, `context`,
`review`, or `unavailable`. An unavailable original needs a `reason`; absent
preferred text needs `gap_reason`. A readable original still gets a byte-identical
copy after recognition fails. Empty OCR alone does not establish that no text
was expected. Record context, alternate captures, identity, and visual evidence
in human README prose.

Optional `complementary_text` is a list of additional `{ "path", "sha256" }`
readings with explicit labels. A nonempty but incomplete preferred reading can
also carry `gap_reason`. Export headers separate original locators, retained-text
provenance, and gap notes from extracted wording.

Filing ordinals are stable positions. Optional `capture_order` is separate.
Only assert `reading_order` with `reading_order_evidence`; the supported sequence
must cover every source in that folder with distinct positions. Otherwise the
source map and combined reading explicitly follow filing order. Later discovery
of an earlier capture does not rename existing copies.

```sh
agent-transcribe file \
  --assignments '/archive/batch-inputs/assignments.json' \
  --output '/archive/organized' --fetch
```

`--source-root` and `--text-root` deliberately override relocated recipe roots.
`--fetch` covers assigned originals/text and required filing controls/outputs.
Copies preserve source bytes, embedded metadata, and modification times. Safe
extension preserves existing names and human prose. Changed originals, chosen
text, managed membership, or published machine outputs stop for inspection.

## Layout and publication

```text
Descriptive document folder/
  README.md
  OCR discovery text.txt
  Scans/
    Description - scan 0001 - Full original filename.jpg
  Source text/
    Description - scan 0001 - Full original filename.jpg.txt
  .filing-current.json
```

The README owns descriptive evidence. The filer maintains only the region
between `<!-- agent-transcribe:source-map:start -->` and
`<!-- agent-transcribe:source-map:end -->`. Human prose outside it remains editable.
Do not edit the map and silently regenerate from earlier assignments.

The current schema-1, assembly-version-1 `.filing-current.json` records
membership, source/export hashes, order and reading provenance. Existing flat
relative paths remain valid. Unsupported receipts and markers are rejected
without conversion.

Publication uses document/destination writer locks, staged bytes, and
`.filing.pending.json`. Resume the same recipe after interruption. Previous notes,
combined reading, and snapshots are retained under `.filing-history/`. Human
prose added during interruption is preserved if the managed map stays unchanged.
Changed outputs/membership, missing staging, or unsafe paths stop publication.
Do not delete receipts, staging, or history to force progress. An intentional
new preferred reading requires [reconciliation](reconciliation.md).

## Check filing separately from recognition

```sh
agent-transcribe check \
  --document '/archive/organized/Selected document' \
  --verify-sources --fetch
```

Repeat `--document` for the affected set. Checks cover membership, named copies,
size/modification time, selected-text hashes, and combined reading/order.
`--verify-sources` additionally hashes readable originals; unavailable originals
remain unverified. `--source-root` supplies a relocated original root. Exact-path
fetch includes originals only with `--verify-sources`; copy verification reads
selected copies. Independently inspect the reported coverage and review evidence.

`file` and `check` return `2` for unavailable originals or text gaps, `1` for
invalid/inconsistent work, and `0` for the selected operation without reported
gaps. Fully accounted-for filing can retain explicit gaps. No result certifies
correct wording, identification, or visual review.

## Select and search

```sh
agent-transcribe reading \
  --document '/archive/organized/Selected document' \
  --query 'packet' --query 'switching' --fetch
```

Without queries the command reports selected paths and coverage. Queries are
case-insensitive literal OR terms. `--limit` bounds matching lines and reports
truncation; `--context` chooses surrounding lines. Matches label human notes
separately from source text. Search spelling variants, abbreviations, and OCR
confusions, then read full passages and adjacent pages.

Default selection is README evidence plus one preferred export per original.
Combined files, raw retry text, and historical exports are not repeated as
additional evidence. Distinct captures stay distinct even with identical words
or bytes. Reading validates current membership/text, but only copied-image
metadata; it does not fetch or hash image payloads. No OCR tools or model are
needed. Apply the [workflow evidence standard](workflow.md#evidence-and-reading)
when using the results for research.

For pending filing add `--job '/archive/organized/Needs review/Selected range'`
explicitly. Successful per-source extracts remain readable while blanks,
failures, and unavailable originals remain gaps. Filed preferred text supersedes
the same original's raw-job text. Ambiguous unfiled variants need an explicit
choice. Results are disposable selections, not another editable catalog.

Gaps/discrepancies return `2` with valid bounded results; integrity/input errors
can stop with `1`. Reading output describes the selected files and terms, not
historical absence or verified wording. The [workflow](workflow.md#evidence-and-reading)
owns source citation, original inspection, and interpretation requirements.
