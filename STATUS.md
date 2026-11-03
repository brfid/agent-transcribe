# Software status

Version 1.1.0rc1 is an unpublished conversion candidate. `convert`, `plan`
and `verify` provide frozen source selection, pinned local Docling processing,
bounded recovery, portable bundles and guarded resume. The compatibility
commands `scan`, `file`, `check`, `reading` and `reconcile` retain existing
accepted work. See [conversion](docs/conversion.md),
[installation](docs/installation.md) and the [workflow](docs/workflow.md).

## Validation

The pre-checkpoint validation record reports 335 synthetic tests with local
models, operating-system network access denied and no skips. This records a
local run; the default CI environment does not install conversion models.
Additional recorded software checks cover offline dependency installation,
source/wheel installation, legacy scan/reading, TXT convert/verify, interruption,
accepted-byte preservation and bundle relocation. These are software checks,
not evidence of archive recognition quality or source-processing completion.

## Limits

Mechanical verification does not establish recognition accuracy, document
identity, historical completeness or visual review. Unknown page/frame totals
remain incomplete. DOCX uses structural locators; accepted text stays preferred,
and unmapped legacy text does not acquire invented physical pages. Optional
unavailable rescue tools and irrecoverable bytes remain explicit gaps. Picture-only
native output does not count as searchable text, while nonempty OCR can still
omit or misread source content. Multiple readable orientation candidates require
inspection.

Storage reservations are conservative estimates, not output-size guarantees.
Native clone copies are opportunistic; logical reservations do not depend on
physical sharing. Handled interruptions resume, but untracked `.pending-*` files
after a hard kill require inspection. Recovery replay evidence is separate from
portable native JSON. Searchable-PDF generation remains in the compatibility
scan path. Exact pinned-file materialization requires explicit `--fetch` and
never relaxes identity checks.

Source scope, execution authorization, review evidence and restart points belong
in the owning archive records. Installing software or preparing a plan does not
authorize processing, migration, deletion or publication.
