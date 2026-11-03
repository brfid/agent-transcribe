# Portable folder conversion candidate

The local unpublished 1.1.0rc1 candidate supplies the normal route for new folder
conversion through `convert`, `plan` and `verify`. Use the separate
[conversion installation](installation.md#conversion-candidate). Existing accepted trees remain readable through the
five legacy commands. Archive records authorize sources and execution; installing
software or generating a plan does not authorize archive processing.

## Select and run a folder

```sh
agent-transcribe-convert convert '/archive/Box 1' --output '/archive/Box 1 converted' --recursive --plan
agent-transcribe-convert convert '/archive/Box 1' --output '/archive/Box 1 converted' --recursive --artifacts-path "$HOME/.local/share/agent-transcribe/models" --fetch
agent-transcribe-convert verify '/archive/Box 1 converted'
```

The first command prints a metadata-only frozen selection without models, source
payload reads, fetching, output publication or inference. It rejects `--fetch`,
`--retry-failed` and inference settings. Nonrecursive selection is the default.
Repeat `--exclude RELATIVE_PATH` for known derivative/tool/control paths. Hidden,
symlink and detected generated-output trees are excluded or rejected. The output
must be separate from every original tree. Unsupported visible files remain
explicit context/gaps; they are never silently handed to another service.

`--sources-file PATH` uses an exact ordered UTF-8 path list instead of a walk;
paths resolve relative to the list. Repeat `--reuse-from EXISTING_ROOT` to import
hash-pinned accepted readings and existing organization read-only. New structure
and mapped OCR are additional candidates. Accepted wording stays preferred;
unmapped multipage legacy text never acquires invented page boundaries.

Rerun the exact execution command to resume. The receipt freezes the original
source list and selection controls; newly appearing files require a fresh plan
and separate destination. Source, retained reading, settings, runtime/model or
accepted-output changes stop execution. Do not remove receipts or work folders
to force a retry. Ordinary handled interruption resumes guarded checkpoints. A hard
kill during temporary publication can leave an untracked `.pending-*` file; the
next run stops for inspection rather than deleting or accepting unknown bytes.
Preserve that evidence and inspect it before proceeding. `--retry-failed` adds a bounded attempt after a completed partial
run and preserves prior accepted bytes.

Unavailable-only attempts leave the runtime unbound. When a recovered source
first requires package parsing or OCR, the runner verifies the previously used
runtime before acquiring that additional requirement and retains the old identity
in `runtime_history`. Requirements never downgrade. Legacy text-only placeholders
can acquire a modeled runtime only if no modeled conversion work exists; actual
package/model drift still stops execution.

If an authorized document-note update follows verified intake, resume with
`--use-pinned-notes` to retain the earlier intake-pinned note snapshot. Its exact
hash, size and filing membership block are verified; current notes are not copied
over or silently adopted. Originals, filing receipts, accepted and linked text
are still checked against their existing pins. Default resume continues to stop
on changed notes. The option cannot recover an unpinned note or change a frozen
selection, accepted reading, model or output. Record the note boundary in the
owning archive and retain the prior receipt before recovery.

## One consolidated plan

```sh
agent-transcribe-convert plan '/archive' --output '/work/conversion-plan.json' --bindings '/work/collection-bindings.json'
agent-transcribe-convert convert --plan-file '/work/conversion-plan.json' --artifacts-path "$HOME/.local/share/agent-transcribe/models" --fetch
```

Bindings are either a JSON list or an object containing `bindings` and an optional
`exclusions` list. A binding supplies a named collection, original root, separate
destination, owning status, exact sources or recursion policy, priority,
reuse roots and `scope_state` (`ready` or `defer`). `prerequisites` contains
validated job-ID dependency edges; those jobs must publish before the dependent
job runs. `readiness_requirements` contains human review/approval conditions,
which must be resolved in the owning archive records before its scope is made
ready. It is separate from the executable dependency list. Paths resolve
under the selected archive root. Unknown material remains visibly deferred until
its scope is established. The planner writes JSON and a readable `.md` beside it.
It imports operating records as evidence; it does not infer authorization from
prose. The runner respects ready/deferred state and prerequisites, rejects overlap,
and runs only the saved exact lists. There is no blanket approval override.

The actions `reuse`, `enrich`, `convert` and `defer` describe required work.
Prior coverage independently records verified complete, partial, no recorded
processing or unresolved. No recorded processing does not prove no earlier work.
Per-bundle receipts own progress; the consolidated runner derives its summary
from them. Deferred material prevents an entirely converted archive claim.

## Outputs, recovery and limits

A standalone bundle contains byte-identical `Originals/`, per-source `Items/`,
preferred `reading.md`, mapped candidate `pages.md` where needed, original
page/frame images, Docling JSON/Markdown, figure/table assets, source maps,
`readings.txt`, `issues.md`, `closeout.md` and `conversion.json`. Native JSON is
absent for plain TXT or salvage without a native document, explicitly recorded.
Internal recovery replay snapshots such as `native-candidate.json.bytes` are
hash-pinned opaque evidence. They are distinct from each candidate's portable
`document.json`, whose image references are exported as relative local assets.
Move the whole bundle to retain its local links. DOCX uses structural locators;
TXT uses lines. Neither invents physical pages. PDF pages and TIFF frames retain
one-based original numbering, including blanks and repeated captures.

One warm CPU inference child processes ranges of 16 with queue size one, a
600-second watchdog and at most one crash restart. Independent rendering is
checkpointed before inference. Finite recovery includes singleton retries,
alternate PDF parsing, qpdf scratch repair, raster rescue, full-page OCR and
orientation candidates, later-frame TIFF decode, partial DOCX salvage, and
strict TXT decoding with labeled legacy candidates. Optional absent tools and
exhausted attempts leave explicit gaps. Integrity, storage and publication errors
stop; they cannot be downgraded to recognition failures. Successful prepared and
accepted outputs survive interruption. Image defaults use scale 3 (216 DPI for
PDF rendering); large collections can consume substantial space. The receipt
records a conservative `storage_estimate` for remaining original copies, page
assets, retained variants, prepared/retry copies and one in-flight repair range.
The runner checks available space before copying and each range. Unknown counts
or dimensions reserve only the next bounded range; the estimate is not an
output-size guarantee. Actual storage/publication failures remain fatal.

`verify` checks stored hashes, paths, links, accounting and decodes stored images.
It reports `coverage_accounted`, `conversion_complete`, `publication_complete`
and recognition warnings separately. Exit codes are 0 for verified mechanical
completion, 2 for partial/unknown coverage or pending publication, 1 for invalid
configuration/integrity/storage/publication failure, and 130 for interruption.
A mechanically valid bundle can retain OCR warnings. Recognition accuracy,
historical identity, source completeness and visual review require original
inspection. No archive-wide conversion is part of software validation.

For newly authorized bundles, `--raster-images-scale 1` explicitly freezes a
separate Docling page/picture image scale for native IMAGE/TIFF inputs and their
raster recovery candidates. The default remains scale 3; omitted settings retain
the earlier frozen receipt/runtime/recovery identities. Changing or removing an
explicit scale on resume is refused. PDF rendering remains scale 3, including
its independent page images. Rasterized PDF recovery candidates use the raster
export policy. OCR settings and retry limits are unchanged. Full-resolution,
EXIF-normalized independent images remain available regardless of native export
scale. Native picture crops use the same scale as their native page images.

Scale 1 gives original pixel dimensions for ordinary 72-DPI camera inputs; it
is a physical point scale, not a universal original-pixel cap. At other recorded
DPIs native exports may shrink or enlarge relative to original pixels. The
original and full independent page/frame images remain the inspection sources.
Synthetic recognition comparisons do not establish archive recognition quality;
inspect consequential original passages. The conservative storage reservation
continues to cover the original independent pixels, native exports and finite
retained recovery variants. No deduplication or earlier receipt migration occurs.

Generated page images can exceed Docling Core's default 20 MiB encoded-image
limit. The isolated worker temporarily uses the already reserved per-unit
DPI-aware raster envelope after checking it against independently decoded render
dimensions. It records the default/effective limit and basis in part provenance,
and restores the default after every task, including failure. File/remote URI
permissions and Pillow decoding protections stay unchanged. Oversized native
payloads beyond the finite task budget remain errors.

## Transparent local copy economy

New conversion work/publication copies may use native Darwin clonefile on the
same supporting volume. Every retained path is an independent inode and remains
a standalone ordinary file: mutating one does not change the others. Unsupported
platform/filesystem and cross-volume clones fall back to ordinary copy2; I/O,
space and permission errors remain fatal. Existing targets retain copy2 behavior
inside fresh trees; exclusive publication still refuses an existing final name.
Clone/copy preserves source timestamps and metadata; atomic publication retains
its private mode 0600. Atomic publication of a source with blocking BSD file flags
(as described below) uses the historical byte-copy path; non-atomic copy2
callers use ordinary copy2 directly for those flags. Thus readable immutable originals remain
supported without propagating their flags. Legacy scan/filing copy routines
are unchanged.

No existing archive object is relinked, deleted or rewritten. Frozen settings,
receipts, hashes, filenames, citations, model behavior and capacity reservations
are unchanged. Physical clone sharing is opportunistic and never a reason to
lower a logical storage reservation or free-space floor. st_blocks sums may count
shared extents repeatedly; volume-wide free-space deltas include other activity.

Darwin document-ID tracking (`UF_TRACKED`,0x40) and visibility hints
(`UF_HIDDEN`,0x8000) do not by themselves disable native cloning. Every
other/unknown BSD flag retains ordinary-copy behavior; immutable/append,
entitlement-protected, nounlink, firmlink and data-less flags stay conservative.
Non-Darwin flag handling is unchanged. This metadata dispatch changes no
receipt setting, source bytes, source flags or logical storage reservation.

## TXT retained line and metadata reservation

TXT budgeting additionally reserves 128 KiB per canonical line plus 64 times
the larger of raw source bytes and JSON-escaped UTF-8 line-text bytes, with
1 MiB per source and all earlier minima retained. This covers the measured
per-line files and repeated receipt/map/text exports; it is a conservative
planning estimate, not a guaranteed output or physical-space bound. No model,
OCR, image rendering, text normalization or output schema change is involved.

The canonical BOM-aware strict decoder supplies line/escaped-byte costs. If it
fails, budgeting uses an explicit raw-byte line/escape upper allowance, preserves
unknown inventory/encoding, and leaves existing failed conversion/salvage gaps
unchanged. It does not infer a legacy encoding or promote decoded text.

Pending/retry TXT profiles from older receipts upgrade monotonically, retaining
the previous profile in storage_profile_history. Complete source profiles and
frozen options are unchanged. Storage failure still stops before original-copy,
rendering, inference or publication replay; checkpoint receipts may be saved.
Independent free-space floors remain archive policy and must not be lowered.

If root views remain pending after sources complete, the reservation separately
covers their line-aware regeneration and exact known new/previous publication
bytes. This does not upgrade complete-source profiles: the extra estimate is
ephemeral and recorded in storage_estimate. It covers interruptions before or
after root-view intent creation without treating accepted sources as new work.


Picture-only native PDF/image/TIFF readings retain their original Markdown and images, but generated image placeholders do not count as searchable text. They receive the no-text warning and the existing single full-page recovery attempt. Native captions, table text and literal source text remain text; a no-text warning does not establish a blank original. Accepted text remains preferred when supplied.

Orientation recovery uses the same preserved native no-text evidence when identifying its sole usable candidate; picture-only variants remain retained but do not create competing readable candidates. Multiple genuine readings remain ambiguous and require inspection.

## Exact pinned output availability

With explicit `convert --fetch`, repeated checks of already pinned originals,
published outputs, render checkpoints and recovery artifacts may materialize one
exact unavailable file at the point of verification. A dataless flag or short
read with unchanged metadata can consume one durable path claim per invocation.
The installed materializer uses a one-file download window and a 120-second
adapter deadline, below the worker watchdog; its normal space reserve remains.
Intent and terminal evidence stay under the sibling work directory. Before each
fetch, immediate free bytes must cover the positive recorded canonical
reservation plus that file; after fetch the reservation must remain available.
Missing reservation or either space failure stops before publication. Existing
receipts supply the initial reservation without rewriting their checkpoint;
fresh jobs acquire it from the normal storage guard.

The expected size, SHA-256 and recorded modification time never change. File
identity must stay stable across materialization and the full reread must match.
Resident full-length hash mismatch, path/symlink/metadata drift, failed fetch or
a second availability failure remain fatal integrity stops. Fresh generated
records require complete stable reads and cannot silently record an empty hash
for a nonempty file. No generic checksum automatically fetches files, no list or
tree prefetch is introduced, and no receipt/runtime reset is permitted.

Availability policy is orchestration state outside frozen backend options. The
existing runtime/model/options comparison still runs unchanged on resume;
materialization does not establish server freshness or recognition accuracy.
