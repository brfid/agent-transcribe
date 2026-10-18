# Installation and external tools

The supported environment is Python 3.11 or later on macOS or Linux. File locking
and process-group cleanup use POSIX facilities. The base package has no Python
runtime dependencies. Fresh photograph OCR imports Pillow only on that path.

## From source

```sh
git clone https://github.com/brfid/agent-transcribe.git
cd agent-transcribe
./scripts/install-local.sh --images
"$HOME/bin/agent-transcribe" --help
```

The installer uses `AGENT_TRANSCRIBE_PYTHON` (default `python3`), a dedicated venv
at `~/.local/share/agent-transcribe/venv`, and a symlink in `~/bin`. It installs the
checkout in editable mode. Add `~/bin` to your PATH if needed. Omit `--images` to
install the base package; rerun with `--images` when photograph OCR is needed.
It does not install external executables or download models.

Override locations explicitly when useful. Venv and launcher-directory overrides
must be absolute paths; relative values are rejected before installation:

```sh
AGENT_TRANSCRIBE_PYTHON=python3.11 \
AGENT_TRANSCRIBE_VENV='/tools/agent-transcribe-venv' \
AGENT_TRANSCRIBE_BIN_DIR='/tools/bin' \
./scripts/install-local.sh --images
```

The script refuses an existing unrelated launcher. Keep the checkout available
for an editable install. To use a normal installation in your own environment:

```sh
python3 -m pip install '/path/to/agent-transcribe[images]'
```

From an uninstalled checkout, `python3 process.py ...` or
`PYTHONPATH=src python3 -m agent_transcribe ...` uses the same commands. Install
Pillow in that interpreter when needed.

## External executables

| Operation | Required tools |
| --- | --- |
| Native TXT/DOCX extraction; file/check/reading/reconcile | Python only |
| Fresh JPG/JPEG/PNG/TIF/TIFF OCR | Tesseract with English data; Pillow `images` extra |
| PDF text and physical-page inspection | Poppler: `pdftotext`, `pdfinfo` |
| Assembled/searchable PDF | Poppler `pdfunite`; OCR tools as required by source |
| Image-only or mixed PDF fallback | OCRmyPDF 17+ plus its system dependencies and Tesseract |
| Exact-path iCloud access | Installed `icmat` |

On macOS, the OCR executables can be installed through Homebrew:

```sh
brew install tesseract poppler ocrmypdf
```

On Debian/Ubuntu:

```sh
sudo apt-get install tesseract-ocr poppler-utils ocrmypdf fonts-dejavu-core
```

Check `ocrmypdf --version`: the runner uses `--mode skip`, introduced in
[OCRmyPDF 17](https://ocrmypdf.readthedocs.io/en/stable/advanced.html#ocr-processing-mode).
A distribution's package may be older. If so, install OCRmyPDF 17 in a dedicated
Python environment and put that environment's `bin` directory on PATH; retain
the system packages for its external dependencies. CI explicitly installs the
17.x Python package after the system tools.

These are setup commands to choose deliberately; the scanner does not run them.
Consult upstream documentation for
[Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html),
[Poppler](https://poppler.freedesktop.org/), and
[OCRmyPDF](https://ocrmypdf.readthedocs.io/en/stable/installation.html).

## iCloud inputs

For files on macOS iCloud Drive, follow the archive's exact-path intake rules and
use `--fetch`. The executable resolves from `AGENT_TRANSCRIBE_ICMAT`, then PATH,
then `~/bin/icmat`. Set the environment variable to an explicit installed path
when necessary. An ordinary local run does not require icmat.

Use `icmat fetch --json -- EXACT_PATH` for readability and `icmat check --json --
EXACT_PATH` for metadata only. Fetching establishes point-in-time readability,
not freshness or historical completeness. Do not recursively fetch the archive.
Control lists, assignment/reconciliation recipes, reuse indexes, and selected
retained extracts must also be readable before the command can consume them;
fetch the exact paths needed. Retain failure receipts and retry the same path,
without broadening source scope.
