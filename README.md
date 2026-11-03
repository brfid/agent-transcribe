# agent-transcribe

Make selected archival sources searchable with ordinary local files, stable
original locators, preserved readings and explicit gaps. Start with existing
preferred text; use bounded `scan` jobs for eligible missing text. Descriptive
filing and full portable Docling conversion are optional routes. Originals and
archive records stay outside this repo.

## Start with a searchable baseline

Use Python 3.11 or later:

```sh
git clone https://github.com/brfid/agent-transcribe.git
cd agent-transcribe
./scripts/install-local.sh --images
"$HOME/bin/agent-transcribe" --help
```

The installer downloads no models or external OCR executables. See
[installation](docs/installation.md) for the tools your formats require.
[Workflow](docs/workflow.md) is the single setup-to-closeout sequence: declare a
finite source set, reuse readings, extract missing text, check access/search and
finish with enumerated exceptions. It includes a generic starter layout and
commands. No filing, duplicate original copies, database or Docling model setup
is required for that baseline.

Read/search explicitly selected existing documents and extraction jobs:

```sh
agent-transcribe reading \
  --document '/archive/organized/Selected document' \
  --job '/archive/jobs/baseline-001' \
  --query 'planning' --query 'plan'
```

Use only the selectors that exist in your archive; `--job` works without a filed
document. Reading selects one preferred text per original and labels notes
separately. It does not search the whole archive automatically. OCR can miss or
change words; inspect originals for quotations and consequential claims.

## Optional richer outputs

Use `file` and `check` for explicit descriptive assignments and verified named
copies; `reconcile` deliberately changes a filed preferred reading. Use the
separate `agent-transcribe-convert` launcher for selected portable bundles with
copied originals, page/frame assets and structured Docling exports. See
[conversion](docs/conversion.md) and its
[isolated installation](docs/installation.md#conversion-candidate).
Version 1.1.0rc1 is the local, unpublished conversion candidate. Installing tools
or preparing a plan does not authorize source processing, migration or deletion.

| Guide | Contents |
| --- | --- |
| [Workflow](docs/workflow.md) | Finite baseline setup, route choice, evidence and completion |
| [Scan](docs/processing.md) | Exact inputs, formats, text reuse, outputs and recovery |
| [Filing and reading](docs/filing-and-reading.md) | Preferred-text search and optional descriptive filing |
| [Conversion candidate](docs/conversion.md) | Portable folder/plan conversion, verification and resume |
| [Reconciliation](docs/reconciliation.md) | Deliberate preferred-text changes |
| [Agents](docs/agents.md) | Delegation, review and handoff |
| [Architecture](docs/architecture.md) | Modules and current formats |

Audio uses [agent-speech-processor](https://github.com/brfid/agent-speech-processor).
See [STATUS.md](STATUS.md) for capabilities and checks. [MIT licensed](LICENSE).
