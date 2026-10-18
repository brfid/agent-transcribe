# agent-transcribe

Extract searchable text from scans and PDFs, file named copies, and read preferred
text. Processing is local. Originals and archive records stay outside this repo.

## Install

Use Python 3.11 or later on macOS or Linux:

```sh
git clone https://github.com/brfid/agent-transcribe.git
cd agent-transcribe
./scripts/install-local.sh --images
"$HOME/bin/agent-transcribe" --help
```

Omit `--images` when photograph OCR is not needed. See [installation](docs/installation.md)
for external OCR tools, path overrides and iCloud setup.

## Use

The commands are `scan`, `file`, `check`, `reading` and `reconcile`. Give them exact
inputs. Follow the archive's local scope and the [workflow](docs/workflow.md) through
filing and checks. OCR can miss or change words; inspect originals for quotations.

| Guide | Contents |
| --- | --- |
| [Workflow](docs/workflow.md) | Batch scope, evidence, filing and completion |
| [Scan](docs/processing.md) | Extraction, text reuse and recovery |
| [Filing and reading](docs/filing-and-reading.md) | Assignments, checks and search |
| [Reconciliation](docs/reconciliation.md) | Deliberate preferred-text changes |
| [Agents](docs/agents.md) | Delegation, review and handoff |
| [Architecture](docs/architecture.md) | Modules and current formats |

Audio uses [agent-speech-processor](https://github.com/brfid/agent-speech-processor).
See [STATUS.md](STATUS.md) for capabilities and checks. [MIT licensed](LICENSE).
