# Extracting scan text

Use `scan` for a lightweight baseline text job over an exact selected source
set. The optional [conversion candidate](conversion.md) instead creates full
portable bundles with page/frame assets and structured exports. Archive records
own the exact authorized scope for either route.

`scan` extracts only an explicit ordered source set. It does not identify or file
documents; filing is optional. Follow the [workflow](workflow.md) for baseline
setup, finite attempt policy and completion. Use a separate derivative folder;
keep originals intact.

```sh
agent-transcribe scan \
  --sources-file '/archive/batch-inputs/ordered-paths.txt' \
  --source-root '/archive/originals' \
  --output '/archive/jobs/baseline-001' \
  --fetch --best-effort
```

The UTF-8 list contains one path per line, relative to the list's directory or
absolute. Blank lines are ignored; spaces and `#` are literal. No globbing,
sorting, recursion, or shell expansion adds inputs. Alternatively repeat
`--source` in source/capture order. Put the list outside the output folder.

Choose `--source-root` explicitly for portable collection-relative locators,
especially when some inputs are unavailable. If omitted on a new job, a common
parent is inferred and retained. A relocated archive can supply a new root without
changing relative source identity. Equal basenames in nested folders stay distinct.

`--fetch` calls installed icmat on exact selected sources and is required for
iCloud originals. Best-effort mode fetches each input separately and retains its
native receipt under `.intake/`, with previous receipts preserved. For control-list/reuse-text intake and executable resolution, follow the
[exact-path installation guidance](installation.md#icloud-inputs).

## Outputs and coverage

| Output | Meaning |
| --- | --- |
| `pages/<original filename>.txt` | Raw extraction for a source directly under its root |
| `pages/by-source/<relative source path>/extract.txt` | Raw extraction for a nested source |
| PDF page text leaves | Original locator and one-based physical page; explicit blank-page marker |
| OCR TSV | Word coordinates from fresh photo OCR |
| `document.txt` | Combined text in declared order when every input extracts successfully |
| `partial-reading-NNNN.txt` | Immutable ordered partial snapshot with every selected source and explicit gaps |
| `README.md` | Scaffold only when absent; human changes stay intact |
| `.processing.json` | Settings, source/output hashes, steps, failures, and checkpoints |

The JSON result/receipt gives exact generated paths. Raw PDF text retains
form-feed boundaries. A README's initial failure list is dated history; the
current receipt/result owns mechanical retry coverage. The `reading_text` result
selects the current complete or partial view. Missing combined text does not
prevent reading successful per-source extracts.

Ordinary discovery omits `--pdf`. That option adds per-source PDFs and assembled
`document.pdf`; it is valid for image/PDF inputs. Optional searchable PDFs are
derivatives, not replacements for original copies. Native TXT and DOCX should
run without `--pdf`. DOCX covers main-body paragraphs/tables with paragraph
locators; headers, notes, comments, and embedded images need separate review.
TXT is decoded as UTF-8 (optional BOM); other encodings need an explicitly
chosen preserved derivative or remain a gap. Image filenames are locators, not
invented document pages. PDF physical-page delimiters are checked against
`pdfinfo`; this does not establish text completeness within a page.

The scan route accepts only TIFFs with one verified frame and valid inspected
frame metadata. Multipage TIFFs and unreadable/unknown frame totals fail closed,
even for reuse or without rotation. The receipt records expected frame IDs where
known and leaves them as gaps; older text is preserved without a new frame-coverage
claim. Choose separately authorized frame-aware conversion for those sources, or
close the baseline with explicit TIFF exceptions. Do not flatten a TIFF to its
first frame and call the original covered. Unsupported inputs become explicit
failures under best-effort extraction.

## Reuse before OCR

`--reuse-index PATH` accepts a current scan `.processing.json` or a hash-pinned
retained-text index. The index has this shape:

```json
{
  "schema": 1,
  "kind": "retained-text",
  "entries": [
    {
      "source": "../originals/Box 2/IMG_1001.jpg",
      "text": "../readings/IMG_1001.txt",
      "source_sha256": "<original SHA-256>",
      "text_sha256": "<retained text SHA-256>"
    }
  ]
}
```

Paths resolve relative to the index unless `--reuse-root` is supplied. Both
hashes are required; changed or missing selected bytes stop reuse. For example:

```sh
agent-transcribe scan \
  --source '/archive/originals/Box 2/IMG_1001.jpg' \
  --source-root '/archive/originals' \
  --reuse-index '/archive/jobs/baseline-001/.processing.json' \
  --output '/archive/jobs/selected-subset' --fetch
```

A prior receipt can supply an exact subset without another extraction or catalog.
Selected original/output hashes are checked. Verification timestamps alone do not
change reuse identity. Conflicting readings require selecting one index; missing
selected text is an explicit error. Blank retained text triggers one fresh attempt
while the older text is preserved. Nonempty text does not prove good recognition.
Plain-text reuse with `--pdf` may need fresh positional OCR for the PDF/TSV layer;
that layer can differ from the retained text.

## PDF fallback and image retries

Native PDF text is tried first. With `--ocr-image-pdfs`, any physical page without
text triggers one local OCRmyPDF skip pass:
`--mode skip --output-type pdf --optimize 0 --jobs 1`. Complete text is then
extracted with `pdftotext -layout`. Native-text pages remain; residual blank pages
are reported. The generated fallback PDF is retained even without `--pdf`.
Retained PDF text with page gaps can also use this fallback. A nonempty corrupt
text layer is not automatically repaired, and blank/graphical pages are not
assumed to contain words.

Photo defaults are English, PSM 3, and EXIF normalization. For a visibly rotated
or sparse page, make one deliberate variant in a fresh output folder:

```sh
agent-transcribe scan \
  --source '/archive/originals/Box 2/IMG_1001.jpg' \
  --source-root '/archive/originals' \
  --output '/archive/jobs/IMG_1001-rotated-retry' --fetch \
  --rotate-clockwise 90 --psm 6
```

Rotation is clockwise after EXIF normalization. PSM 6 is a uniform text block;
PSM 11 is sparse text. Retry settings apply to every selected image and cannot
combine with non-image inputs or retained-text reuse. Preserve original/variant
bytes, record the reason and preferred reading, and stop after the targeted retry
unless another specific approach is chosen. Faint text and handwriting may remain
unresolved.

## Recovery and exit status

Repeat the same command to verify/reuse completed work and retry independent
failures. Changed sources/settings, altered or untracked outputs, duplicate
inputs, source/output overlap, and simultaneous writers are rejected. Machine
publication uses exclusive creation, bounded subprocess timeouts, and process-group
cleanup. Only mechanical receipts are replaced; `.processing.previous.json`
retains the preceding run. Normal intake retains `icmat-receipt.json`.

Prepared bytes/hashes are checkpointed in `.stage-*` before final publication.
After that checkpoint, resume uses those exact bytes, including PDFs with volatile
creation timestamps, without repeating OCR. Missing/changed/untracked staged
files stop the run. Published staging is removed only after the completed receipt
is saved. Earlier interrupted staging remains evidence and is not routine cleanup.
An interruption before preparation may need extraction again; missing prepared
bytes cannot be reconstructed as the original checkpoint.

Scan receipts require schema 2, adapter 4 and source-root-relative identities.
Unsupported receipts are rejected without conversion. Preserve earlier recipes
and outputs; explicitly selected retained text can be supplied through the
versioned index above. Readable originals are hashed on entry and exit;
unavailable inputs remain intake failures. Coarse intake/extraction timing
is instrumentation, not a throughput benchmark.

Exit `0` means all selected inputs completed mechanical extraction, possibly with
flagged blanks. Exit `2` means best-effort intake/extraction gaps remain with
successful text retained. Exit `1` means invalid inputs, integrity error, or a
nonrecoverable tool/storage/publication failure. Ctrl-C returns `130`. None
establishes recognition accuracy, visual review, or backup coverage.
