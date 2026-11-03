# agent-transcribe

Convert selected archival folders into portable local readings, page/frame images
and structured Docling documents, with explicit gaps and resumable checkpoints.
Originals and archive records stay outside this repo.

## Install the conversion candidate

Use Python 3.11 for the pinned conversion environment:

```sh
git clone https://github.com/brfid/agent-transcribe.git
cd agent-transcribe
./scripts/install-convert.sh
"$HOME/bin/agent-transcribe-convert" --help
```

See [installation](docs/installation.md#conversion-candidate) for deliberate model
setup, external tools, path overrides and iCloud intake. The separate legacy
installer remains available for accepted scan/filing work.

## Conversion candidate

Version 1.1.0rc1 is the local, unpublished candidate for new folder conversion.
It uses Docling, frozen archive plans, bounded recovery and bundle verification.
See [conversion](docs/conversion.md). Installing it or preparing a plan does not
authorize processing; archive records still own the exact permitted scope.

## Use

Use `convert` for new selected folders, `plan` for a consolidated frozen work list,
and `verify` for portable bundles. The compatibility commands `scan`, `file`,
`check`, `reading` and `reconcile` preserve existing accepted material. Follow the
archive's local scope and the [workflow](docs/workflow.md). OCR can miss or change
words; inspect originals for quotations.

| Guide | Contents |
| --- | --- |
| [Workflow](docs/workflow.md) | Batch scope, evidence, filing and completion |
| [Conversion candidate](docs/conversion.md) | Folder/plan conversion, portable outputs and resume |
| [Scan](docs/processing.md) | Extraction, text reuse and recovery |
| [Filing and reading](docs/filing-and-reading.md) | Assignments, checks and search |
| [Reconciliation](docs/reconciliation.md) | Deliberate preferred-text changes |
| [Agents](docs/agents.md) | Delegation, review and handoff |
| [Architecture](docs/architecture.md) | Modules and current formats |

Audio uses [agent-speech-processor](https://github.com/brfid/agent-speech-processor).
See [STATUS.md](STATUS.md) for capabilities and checks. [MIT licensed](LICENSE).
