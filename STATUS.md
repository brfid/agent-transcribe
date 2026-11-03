# Software status

The supported baseline reuses preferred text and extracts eligible gaps with
exact-source `scan` jobs. `reading` searches selected jobs without requiring
filing. Descriptive filing and portable Docling bundles are optional; see the
[workflow](docs/workflow.md). Version 1.1.0rc1 remains an unpublished candidate.

## Validation

On 2 November 2026, Python 3.11 on macOS passed:

- **343 synthetic tests, no skips**, in the pinned conversion environment with
  local models and operating-system networking blocked.
- **288 tests passed; 55 optional checks skipped**, in the lightweight image
  environment without Docling, also with networking blocked.

Checks cover exact scope, reuse, interruption/resume, source preservation,
preferred-text search, saved TIFF coverage gaps and portable bundle relocation.
The default CI does not install conversion models. These local results do not
claim a fresh Linux CI run or recognition accuracy on archival material.

## Limits

Search reports recorded selected-text coverage; it does not audit current
original availability or invent missing page/frame inventories. The lightweight
scan route rejects multipage or uncountable TIFFs with explicit gaps. Optional
conversion supplies frame-aware processing. Nonempty OCR can still omit or
misread text, and accepted unmapped readings do not acquire invented page labels.
DOCX extraction uses structural locators rather than physical pages.

Storage reservations are estimates. Handled interruptions resume; inconsistent
or untracked staging requires inspection. Exact pinned-file materialization is
opt-in and never relaxes identity checks. See [processing](docs/processing.md)
and [conversion](docs/conversion.md) for their distinct output/recovery contracts.

Source scope, authorization, review evidence and restart points belong in the
archive's own records. Software installation does not authorize processing,
migration, deletion or publication.
