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

For a repeatable setup, record the tested Git commit (`git rev-parse HEAD`),
`agent-transcribe --version`, Python and external-tool versions in the archive's
run record. Use that commit on another machine and validate it with synthetic
inputs before updating an existing workflow. Keep the source checkout and its
local changes intact when updating an editable installation.

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

## Conversion candidate

The candidate uses a **separate environment and launcher**. The baseline installer
above remains available. Python 3.11 is the tested runtime; use that version for
the released dependency lock. Other Python/platform combinations need their own
validation. `requirements-convert.lock` pins and hashes all 104 Python packages,
including Docling 2.142.1, docling-core 2.103.1, docling-parse 7.22.4 and Pillow
12.3.0. ReportLab is included for synthetic validation. The optional `convert`
extra provides direct API pins; use the lock for the reproducible environment.

```sh
AGENT_TRANSCRIBE_PYTHON=python3.11 ./scripts/install-convert.sh
"$HOME/bin/agent-transcribe-convert" --version
```

Override `AGENT_TRANSCRIBE_CONVERT_VENV` and
`AGENT_TRANSCRIBE_CONVERT_BIN_DIR` with absolute paths. The default environment is
`~/.local/share/agent-transcribe/convert-venv`. The script refuses unmarked
existing environments, unrelated launchers and the configured legacy environment.
It installs dependencies and the editable checkout; it downloads no model assets.
Both editable launchers import the same source checkout, so both `--version`
commands reflect its candidate source version. The old environment retains its
installed distribution metadata and dependencies; the conversion installer does
not replace them.
For offline Python installation, first obtain a wheelhouse deliberately, then set
`PIP_NO_INDEX=1 PIP_FIND_LINKS=/absolute/wheelhouse` for the same installer. The
hash-locked dependency closure was also validated with `uv pip sync
--require-hashes --offline` in a fresh environment.

Conversion uses Tesseract English and OSD data. Install Poppler and qpdf for
independent PDF counting/rendering and bounded PDF recovery. OCRmyPDF belongs
to the compatibility scan path; it is not routine conversion infrastructure.

### Deliberate model setup

Perform this explicit setup step with network access before conversion. The
conversion command never downloads models. Use the isolated interpreter below
(or your overridden conversion environment path):

```sh
"$HOME/.local/share/agent-transcribe/convert-venv/bin/python" - <<'PYTHON'
from pathlib import Path
from docling.utils.model_downloader import download_models
models = Path.home() / '.local/share/agent-transcribe/models'
download_models(output_dir=models, with_layout=True,
                with_tableformer=True, with_code_formula=False,
                with_picture_classifier=False, with_rapidocr=False)
print(models)
PYTHON
```

The pinned downloader fetches layout and TableFormer assets only (including its
ONNX layout and fast TableFormer siblings). Runtime requires these local files:

- `docling-project--docling-layout-heron/config.json`
- `docling-project--docling-layout-heron/preprocessor_config.json`
- `docling-project--docling-layout-heron/model.safetensors`
- `docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json`
- `docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors`

The validated snapshots were Heron `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`,
ONNX sibling `40bde044036bb181c130ddf6c51792187268748f`, and TableFormer
`fc0f2d45e2218ea24bce5045f58a389aed16dc23`. The downloader resolves upstream
snapshots at setup time, so record the actual revisions, terms and hashes if they
change. Heron is Apache-2.0; TableFormer is CDLA-Permissive-2.0; Tesseract data is
Apache-2.0. The tested assets occupied about 669 MiB; the first Python environment
about 1.2 GiB. These observations are platform-specific, not capacity guarantees.

Preflight hashes local assets and checks pinned package versions and Tesseract
English/OSD before inference. TXT needs no models; DOCX needs the pinned package
runtime but no layout/table weights. An explicit path is required for image/PDF
conversion. A read-only setup check without inference or network access is:

```sh
"$HOME/.local/share/agent-transcribe/convert-venv/bin/python" - <<'PYTHON'
import json
from pathlib import Path
from agent_transcribe.docling_backend import BackendOptions, preflight
models = Path.home() / ".local/share/agent-transcribe/models"
result = preflight(BackendOptions(artifacts_path=str(models)))
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['ready'] else 1)
PYTHON
```

Run conversion and synthetic validation with network denied by the operating
system when an enforced offline boundary is required. Hugging Face offline flags
alone are not an operating-system network barrier. Do not download broad model
families, VLMs or remote plugins for this route. See [conversion](conversion.md)
for selection, outputs and verification.
