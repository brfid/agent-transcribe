# Components and persisted contracts

`agent-transcribe` consumes explicit files and recipes. Archival judgment, source
authorization, and collection progress remain in the archive's own records; see
the [workflow](workflow.md) for that boundary.

| Component | Owns |
| --- | --- |
| `cli.py` | Options, dispatch, bounded reading-result formatting |
| `common.py` | Exact-path intake, hashes, subprocesses, locks, checkpoints, resume |
| `scans.py` | Native/OCR extraction, page locators, reuse, partial views |
| `filing.py` | Assignment publication, validation, preferred-text selection, pinned reconciliation, shared recovery |

`process.py`, `python -m agent_transcribe`, and the installed console command call
one CLI. The base package uses the standard library; Pillow is lazy and optional.
External executables are listed in [installation](installation.md). There is no
embedded agent loop or model SDK.

## Data flow

Explicit selection -> exact-path intake/hash -> reuse/native text/OCR ->
checkpointed extraction -> explicit assignments -> checked named copies and
preferred exports -> reading/search. Later reading changes use a pinned
reconciliation recipe. Filing reuses adequate text rather than rerunning OCR.

Document READMEs own descriptive evidence and source membership. Hidden receipts
own mechanical settings, hashes, steps, and failures; they are not another
catalog. Default reading selects one preferred export per original. Combined
readings and preserved variants remain available without becoming duplicate
default evidence. Collection and document records link canonical guidance instead
of copying software procedures.

## Current formats

Scan receipts use schema 2, adapter 4 and source-root-relative identities.
Filing snapshots use schema 1, kind `scan-filing`, assembly version 1, in
`.filing-current.json`. Assignment, reconciliation and retained-text index
recipes use schema 1. Unsupported formats are rejected without conversion.
Existing flat relative paths remain valid source data.

Managed README regions use:

```text
<!-- agent-transcribe:source-map:start -->
<!-- agent-transcribe:source-map:end -->
```

Source/page locators, filenames, accepted text and preserved evidence remain
stable. A format change does not establish recognition accuracy or review.

Publication stages exact bytes before guarded final creation, checks original
and output identities, rejects competing writers, and retains history. Extraction
and filing have distinct checkpoints and coverage claims. Recovery details belong
to the [scan](processing.md#recovery-and-exit-status),
[filing](filing-and-reading.md#layout-and-publication), and
[reconciliation](reconciliation.md#interruption-and-limits) guides; changing a
software identity must not rewrite earlier archival evidence.
