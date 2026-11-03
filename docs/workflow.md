# Processing and descriptive filing workflow

The goal is best-effort searchable text with stable original/image/PDF-page
locators, preserved readings, and explicit gaps. An LLM can discover and read it
with ordinary local files and search tools. Discovery text is not a verified
quotation source.

The archive's local instructions and collection records control authorization,
priorities, destinations, naming, and restart points. This guide supplies the
reusable sequence. An authorized scan-processing batch includes descriptive
filing in the same run unless the user explicitly requests a narrower task.
Availability of this workflow does not authorize processing another collection.

## Choose the route

Use the local [conversion candidate](conversion.md) for new folder conversion:
metadata-only selection, then authorized `convert`, followed by `verify` on the
portable bundle. It handles provisional organization, page/frame assets,
structured exports, bounded recovery and publication without a separate manual
assignment recipe. An existing accepted reading stays preferred when its
hash-pinned provenance is imported with `--reuse-from`.

Use `scan`, `file`, `check`, `reading` and `reconcile` for existing accepted
legacy work and explicitly requested compatibility tasks. Keep accepted source
membership and readings intact. Neither installation nor planning authorizes
source migration, deletion or archive-wide execution. The consolidated runner
executes only the exact sources of ready jobs and respects prerequisites.

## Declare and prepare

Read the archive root instructions/index, then the selected collection status,
plans, READMEs, existing indexes/manifests, and review queue. Resolve recorded
pending filing/dependencies before starting a dependent conversion scope.
For a new collection, record its exact scope, source order,
separate derivative destination, naming choices, and restart point; link that
status from the archive root index. Keep source and derivative destinations apart.

Declare the purpose, exact ordered source list, derivative destination, stopping
point, any open-document handoff, and next source before running. A folder plan
freezes the selected paths; use an exact source list for a
bounded ordered subset and explicit exclusions for derivatives/context. Select
whole PDFs for folder conversion. Legacy `scan` has no PDF page-range option;
a page-limited compatibility task needs existing page text or an explicitly
recorded range derivative. Reserve time for verification and closeout within
the original deadline.

Exclude organized copies, retained jobs, tool directories, hidden/bookkeeping
files, plans, receipts, and indexes from original-source selection. Metadata
enumeration is not processing. Do not extend a batch to finish an apparent
document; carry the open document forward by exact source locators. Inspect the
open-document handoff and the first new source before deciding continuity.

Inventory selected source-relative paths, formats, sizes, readability, and hashes
when readable. Check free space for originals, temporary OCR, staged outputs, and
published copies. Fetch only exact selected iCloud paths; unavailable placeholders
are failures, not processed sources. Preserve original files, capture order,
metadata, enclosures, contextual images, alternate captures, and duplicates.

## Extract and identify

Use [convert](conversion.md) for the authorized frozen folder or ready archive
plan. It retains accepted readings, independently counts/renders physical units,
and tries its finite local recovery policy automatically. Keep successful text
and partial views when individual units/sources fail. Report blank text/pages
and failures separately from completion. `--retry-failed` adds another bounded
attempt while preserving accepted bytes. For compatibility `scan`, follow its
[reuse and targeted retry guide](processing.md). Never invent text to fill a gap.

Inspect originals for decisive titles, dates, boundaries, continuation evidence,
and relationships. Separate printed titles from supplied descriptions. Photograph
or filename dates, declassification stamps, and incidental years are not document
dates. Unknown dates stay unknown and do not alone prevent grouping. Record the
actual pages/covers/transitions inspected; machine confidence is not visual review.

Use a provisional descriptive folder when identification remains uncertain.
Detailed identification must not block searchable text or filing. Each selected
source needs an explicit document, context, review, or unavailable disposition.
A readable original still gets a named copy if recognition failed. Keep repeated
captures as distinct captures and identify their relationship without presenting
them as independent corroboration.

## Publish and check

Conversion publishes a standalone bundle with byte-identical originals, readable
items, page/frame images, native Docling outputs when available, source maps,
accepted-reading provenance, and explicit gaps. Existing organization supplies
browse links; uncertain historical identity stays provisional. A new conversion
does not require preparing descriptive assignments first. Run `verify BUNDLE`,
then independently check original identity, expected-unit accounting, links and
the claimed review scope. Recognition accuracy still requires original inspection.

### Existing legacy filing

For an authorized legacy `file` operation, use the collection's naming convention.
A generic folder may be
`Subject or title - Identifier or part - Supported date`; omit unsupported parts.
New copies use `Description - scan NNNN - Full original filename.ext`, retaining
the full original filename verbatim at the end, including date/time. The stable
ordinal is filing position, not physical page number or chronology. Keep capture
order separate. Appending an earlier capture does not renumber prior copies.
Preserve original bytes, embedded capture metadata, and filesystem modification
times in the named copies.

Each document folder's README owns description, full/supplied title evidence,
date evidence, contents, relationships, original membership/locators, checked
spans, and open questions. Begin with a short description and reading links.
Capture metadata and dated batch history belong below the evidence. Browse lists
link the same document folders rather than creating another physical hierarchy.
No new catalog or invented document identifiers are needed.

Use explicit [file assignments](filing-and-reading.md), matching retained text
exports, and the combined reading. A supported reading order must cover every
participating source with distinct positions and evidence; otherwise say that
combined text follows filing order. Keep old flat layouts stable. PDFs retain the
full byte-identical source copy and one-based physical-page locators; any item or
range derivative is additional and identifies its exact source pages.

Run `check --verify-sources` on the affected legacy document folders.
Independently verify source
coverage, hashes/metadata of copies, original filename suffixes, page/source order,
readable text, gaps, and the claimed review scope. Resolve questions in existing
review queues and plans when evidence warrants it. Use [reconcile](reconciliation.md)
for deliberate changes to a filed reading, rather than editing machine text,
managed membership, or receipts by hand.

## Evidence and reading

For a new bundle, start with `readings.txt` and the preferred `reading.md` links;
`pages.md` identifies mapped candidates separately when accepted text lacks page
boundaries. Use [preferred-text selection](filing-and-reading.md#select-and-search)
for existing legacy folders. Search spelling variants, abbreviations, similar terms,
and OCR confusions; inspect full passages and adjacent pages rather than relying
on snippets. Descriptions and questions in notes are not source wording. Repeated
exports or related captures are not independent corroboration.

Cite original image filenames, or PDF filenames plus one-based physical pages;
add printed page labels only when established. Distinguish machine readings,
contextual interpretation, and visually checked wording. Inspect decisive originals
before quoting or relying on consequential names, numbers, or historical claims.
Retain unknown metadata, adverse evidence, and unresolved readings. A failed query
establishes only that the stated terms did not match the selected readable files;
an unavailable or incompletely read file is not negative evidence.

## Closeout and resume

Record three separate results:

1. Discovery extraction: successful source/page text, tool completion, blank
   pages, unavailable sources, and failures.
2. Identification/grouping review: inspected spans, evidence, provisional choices,
   unresolved titles/dates/boundaries, and alternate-capture relationships.
3. Filing: every selected source accounted for, copy/text publication and checks,
   explicit gaps, and pending work.

A best-effort batch can be fully accounted for while retaining recognition or
access gaps. Copy/integrity/storage/publication failure leaves filing incomplete.
Do not call retained gaps fully extracted or fully visually reviewed. Update the
collection status with date, exact source range, verified counts/outputs, gaps,
and exact next action. Detailed evidence belongs in document READMEs and existing
provenance records. The archive root remains an index of collections/shared plans.

On interruption, preserve successful outputs, receipts, staging, and history;
record pending filing and the restart point. Rerun the exact command to resume.
Independent source failures may continue under best-effort extraction. Changed
originals/settings, modified/untracked outputs, unsafe paths, conflicting writers,
or publication failures require inspection. Never delete a receipt or stage to
force progress.
