# Searchable archive workflow

The baseline is a finite set of preserved originals, browsing links and preferred
search text, with explicit exceptions. Ordinary local files and search tools are
sufficient. Detailed identification, descriptive filing and portable Docling
bundles are optional improvements; they do not have to cover the whole archive
before the baseline can finish. Discovery text is not a verified quotation source.

Archive instructions and collection records own authorization, source scope,
priorities, destinations and restart points. This guide is the reusable sequence,
not permission to process another collection. Existing local requirements and
accepted readings remain authoritative.

## Choose the route

- **Browse/search first:** reuse existing native or accepted text and existing
  collection/document links. `reading` selects explicitly named filed documents
  and/or scan jobs without fetching image payloads.
- **Fill eligible text gaps:** `scan` extracts an exact ordered source set into a
  separate job. It supports native TXT/DOCX/PDF text and local image/PDF OCR;
  no filing or copied-original bundle is required. Check its
  [format and TIFF limits](processing.md#outputs-and-coverage) before selection.
- **Optional descriptive filing:** `file` publishes reviewed assignments, named
  original copies and preferred exports; `check` verifies them. Detailed titles,
  dates, groupings and topic routes can be developed selectively.
- **Optional portable conversion:** the isolated [conversion candidate](conversion.md)
  supplies copied originals, physical page/frame assets, structured Docling
  exports, bounded recovery and bundle verification. Use it when that contract is
  needed, including separately authorized frame-aware TIFF work. Accepted readings
  stay preferred when imported with hash-pinned `--reuse-from` provenance.

Neither setup nor planning authorizes archive-wide execution, migration or deletion.

## Declare and prepare

Read the archive root instructions/index, then the selected collection status,
linked plan, READMEs, existing manifests/indexes and review queue. Resolve only
dependencies of the selected work; unrelated enrichment is not a baseline gate.
For a new collection, record its exact scope, source order, separate derivative
destination and restart point in collection records, and link its status from the
archive index. Reuse those records instead of creating another editable catalog
or inventing document identifiers.

Freeze the release's selected original paths and exclusions. Reconcile them with
existing source records before claiming whole-folder coverage; later additions
belong to a later scope. Exclude organized copies, OCR jobs, tools, receipts,
indexes and other bookkeeping from original-source selection. Keep duplicates,
enclosures and contextual items with their original relationships and order.

Account for every selected source using the existing records: original access,
preferred text, attempt state, expected native-unit coverage where known, and any
unsupported/context/provenance disposition. Keep this separate from descriptive
review and filing. Unknown legacy page/frame counts stay unknown until checked.
A nonempty text file is not evidence of adequate recognition or complete coverage.

Before execution, declare the exact ordered missing-text set, output job, stopping
point, capacity allowance and finite retry policy. A reasonable baseline policy is
one extraction attempt, with a recorded bound on technical resumes and at most one
chosen OCR variant for a demonstrated problem; choose that policy before running.
An untouched eligible backlog cannot be closed under a generic gap label. Inspect
one representative supported extraction and browse/search path when establishing
the method; repeat only when changed conditions or failures require it.

Check space for selected downloads, temporary OCR and retained outputs. Fetch only
explicitly authorized exact cloud paths. A path or placeholder proves neither
readability nor retrievability. Preserve originals, accepted readings, receipts,
failed attempts and variants; no cleanup or eviction is implied.

### Generic starter

Use existing archive layout and names when present. A new small collection can
keep controls and derivatives outside originals:

```text
/archive/
  README.md                 # collection links, preferred-text routes, exceptions
  originals/Box 2/           # preserved source files
  batch-inputs/baseline-001.txt
  jobs/baseline-001/         # scan outputs and mechanical receipt
  organized/                # optional descriptive filing
```

The collection's existing status or plan records the frozen scope, retry budget
and outcomes. The README links it and selected originals/readings/jobs. It need
not duplicate membership or mechanical receipts.

After [installation](installation.md#from-source), put only the authorized missing
sources in the UTF-8 list, one path per line. For example:

```text
/archive/originals/Box 2/IMG_1001.jpg
/archive/originals/Box 2/report.pdf
```

Run the baseline without optional PDF assembly:

```sh
agent-transcribe scan \
  --sources-file '/archive/batch-inputs/baseline-001.txt' \
  --source-root '/archive/originals' \
  --output '/archive/jobs/baseline-001' --best-effort

agent-transcribe reading \
  --job '/archive/jobs/baseline-001' \
  --query 'planning' --query 'plan'
```

For iCloud inputs, follow [exact-path intake](installation.md#icloud-inputs) and
add `--fetch`; obtain only the required controls/text for reading. See
[processing](processing.md) for hash-pinned reuse, PDF OCR fallback and exact
resume commands. Search accepted filed material with repeated `--document` and
unfiled text with repeated `--job`; a filed preferred reading takes precedence
for the same original. No `file` operation is necessary to read a scan job.

## Evidence and reading

Search spelling variants, abbreviations and OCR confusions, then read full
passages and adjacent material. Open the actual selected original at its recorded
locator for decisive wording, names, numbers, dates and historical claims. Verify
representative tasks from finding a passage through opening that source and
adjacent material; state exactly which formats and spans were inspected. Sampling
and successful commands do not establish accuracy for uninspected sources.

Use original image filenames, PDF filenames plus one-based physical pages, or
native TXT line/DOCX paragraph locators where established. Printed page labels are
additional evidence, not substitutes for physical pages. Extraction/search result
line numbers are not automatically original page or line numbers. Never assign
physical pages to an unmapped legacy reading. Keep machine text, descriptive
notes, interpretation and visually checked wording distinct. Repeated exports
and alternate captures are not independent corroboration. A failed query shows
only that the stated terms did not match the selected readable files.

For portable bundles, start with `readings.txt` and preferred `reading.md` links;
`pages.md` keeps mapped candidates separate where accepted text lacks boundaries.
For other readings, use [preferred-text selection](filing-and-reading.md#select-and-search).

## Optional curation and publication

Inspect originals for decisive title/date/boundary evidence before asserting
identity or grouping. Record actual inspected covers, pages and transitions.
Filename/capture dates, stamps and incidental years are not document dates;
unsupported dates stay unknown. Preserve uncertain identity and continuation as
provisional rather than inventing certainty to finish filing.

Use [filing assignments](filing-and-reading.md) for explicit descriptive folders,
source copies and preferred text. Each document README owns description,
source membership/locators, title/date evidence, relationships and review questions;
browse lists link those same folders. Follow the collection's naming convention.
Supported reading order needs evidence for every participating source; otherwise
label combined text as filing order. A full PDF remains byte-identical; a page-range
derivative is additional and identifies its exact physical source pages.

Run `check --verify-sources` on affected filed folders and independently check
coverage, hashes/metadata, locators, order, gaps and claimed review spans. Use
[reconciliation](reconciliation.md) for deliberate filed-reading changes. Portable
conversion instead follows its own publication and `verify BUNDLE` contract,
including copied-original identity, expected units, links and stored assets.
Mechanical integrity does not certify recognition or historical identity.

## Closeout and resume

A baseline can finish **with enumerated exceptions** when:

1. The frozen selected set is reconciled and every source has an explicit outcome.
   Reused text is distinguished from new attempts, unsupported/context material,
   unavailable originals and provenance holds. No eligible source remains silently
   unattempted; deliberate deferrals remain open work.
2. Every declared attempt has a retained receipt/result. Expected source/page/frame
   or other native units are checked where supported; unknown counts remain gaps.
   Report successful, blank, failed, poor-text and unsupported outcomes separately.
   An unsuccessful attempt closes only as an exception, never successful extraction.
3. Browsing and preferred-text search have been checked against selected originals,
   with inspected spans and limitations recorded. All-original availability needs
   actual access evidence across that scope; unresolved access failures prohibit
   that claim even when a baseline with exceptions is closed.
4. Owning collection records retain exact counts/outputs, gaps, measured downloads,
   new output/peak storage and timings where measured, plus the next maintenance or
   restart point. Detailed evidence stays in existing provenance/document records;
   the archive root remains an index.

Report extraction/access coverage, identification/grouping review and filing as
separate results. Optional curation, perfect transcription, handwriting, tables
and Docling enrichment do not reopen a completed declared baseline. New sources
or chosen improvements become another bounded task.

On interruption preserve successful outputs, receipts, staging and history;
record actual pending work and the exact restart point. Repeat the original
command within the declared retry budget. Independent source failures may
continue under best-effort extraction. Changed originals/settings, modified or
untracked outputs, unsafe paths, conflicting writers and storage/publication
failures require inspection. Never delete evidence to force progress. For
optional filing/conversion, an integrity or publication failure leaves that
operation incomplete even when earlier text remains usable.
