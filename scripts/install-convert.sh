#!/bin/sh
# Separate candidate runtime; never modify the installed legacy environment.
set -eu
case "${1-}" in
    "") ;;
    -h|--help)
        printf '%s\n' 'Usage: scripts/install-convert.sh' \
          'Environment: AGENT_TRANSCRIBE_PYTHON, AGENT_TRANSCRIBE_CONVERT_VENV, AGENT_TRANSCRIBE_CONVERT_BIN_DIR' \
          'Optional offline wheels: PIP_NO_INDEX=1 PIP_FIND_LINKS=/absolute/wheelhouse'
        exit 0 ;;
    *) printf '%s\n' "Unknown option: $1" >&2; exit 2 ;;
esac
[ "$#" -eq 0 ] || { printf '%s\n' 'Unexpected arguments' >&2; exit 2; }
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=${AGENT_TRANSCRIBE_PYTHON:-python3}
venv=${AGENT_TRANSCRIBE_CONVERT_VENV:-"$HOME/.local/share/agent-transcribe/convert-venv"}
bin_dir=${AGENT_TRANSCRIBE_CONVERT_BIN_DIR:-"$HOME/bin"}
case "$venv" in /*) ;; *) printf '%s\n' 'AGENT_TRANSCRIBE_CONVERT_VENV must be an absolute path' >&2; exit 2 ;; esac
case "$bin_dir" in /*) ;; *) printf '%s\n' 'AGENT_TRANSCRIBE_CONVERT_BIN_DIR must be an absolute path' >&2; exit 2 ;; esac
launcher="$bin_dir/agent-transcribe-convert"
"$python" -c 'import sys; sys.exit("Python 3.11 or later is required") if sys.version_info < (3, 11) else None'
# Protect the normal installation even if an override points to its environment.
"$python" - "$venv" "${AGENT_TRANSCRIBE_VENV:-$HOME/.local/share/agent-transcribe/venv}" "$HOME/bin/agent-transcribe" <<'CHECK'
from pathlib import Path
import sys
candidate, legacy, launcher = map(Path, sys.argv[1:])
if candidate.resolve() == legacy.resolve():
    sys.exit("Conversion environment must differ from the legacy environment")
if launcher.exists() and launcher.resolve().parent.parent == candidate.resolve():
    sys.exit("Conversion environment is used by the legacy launcher")
if candidate.is_symlink():
    sys.exit("Conversion environment cannot be a symlink")
if candidate.exists() and not (candidate / ".agent-transcribe-convert").is_file():
    sys.exit("Refusing existing unmarked conversion environment; choose a fresh path")
CHECK
if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    if [ ! -L "$launcher" ] || [ "$(readlink "$launcher")" != "$venv/bin/agent-transcribe" ]; then
        printf '%s\n' "Refusing to replace unrelated launcher: $launcher" >&2; exit 1
    fi
fi
"$python" -m venv "$venv"
printf '%s\n' 'agent-transcribe conversion candidate' > "$venv/.agent-transcribe-convert"
"$venv/bin/python" -m pip install --require-hashes -r "$repo/requirements-convert.lock"
"$venv/bin/python" -m pip install --no-deps --no-build-isolation --editable "$repo"
mkdir -p "$bin_dir"
if [ ! -L "$launcher" ]; then ln -s "$venv/bin/agent-transcribe" "$launcher"; fi
"$launcher" --help
printf '%s\n' "Installed candidate: $launcher" 'Models and external executables require deliberate separate setup.'
