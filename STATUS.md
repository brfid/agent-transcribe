# Software status

Updated 2026-10-17. Archive records own source scope, progress and next action.

The toolkit supports explicit ordered extraction, retained-text reuse, PDF-page
locators, partial readings, guarded resume, descriptive filing, copy checking,
preferred-text search and pinned reconciliation. See the [guides](README.md).
Only the [current formats](docs/architecture.md#current-formats) are supported.

## Validation

The current candidate passed 118 synthetic tests, covering OCR,
current-format rejection, preservation, interruption, reconciliation and command
integration. Source/wheel builds and clean base-install workflows passed, including
a source installation in a path with spaces. No archive data was used. Hosted CI
checks pushed commits; confirm its result for the selected release.

## Limits

Tests do not establish OCR accuracy, collection throughput, completeness or
visual review. PDF page-range selection is unavailable. DOCX extraction covers
main-body paragraphs and tables. Reading checks copied-image metadata without
fetching or hashing image payloads. Recognition gaps remain explicit.
