# Working on agent-transcribe

Read [STATUS.md](STATUS.md), the [workflow](docs/workflow.md) and the guide for your
task. Archive-local instructions and records own source scope, destinations,
descriptive evidence, progress and next action. Read them before processing.

Follow the complete [agent protocol](docs/agents.md) for economical delegation,
bounded assignments, write ownership, independent review and handoff.

Keep originals, derivatives, receipts, private metadata and credentials outside
Git. Use synthetic fixtures. Preserve originals and accepted text; a software
change does not authorize new processing. Use only current formats and interfaces.
Record decisions in the archive files that own them.

[Architecture](docs/architecture.md) defines module boundaries.
Update the owning guide when behavior changes; link it instead of copying procedures.

## Checks

Create a development environment and install the image extra. The OCR tests
also need the [external tools](docs/installation.md#external-executables).

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[images]'
python -m unittest discover -s tests -v
git diff --check
```

Use synthetic data. Check preservation, interruption and output contracts when
changing behavior. Keep private archive files out of tests and release artifacts.
