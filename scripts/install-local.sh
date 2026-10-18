#!/bin/sh
# Install this checkout without installing OCR executables or downloading models.
set -eu

images=no
case "${1-}" in
    "") ;;
    --images) images=yes; shift ;;
    -h|--help)
        printf '%s\n' 'Usage: scripts/install-local.sh [--images]' \
          'Optional environment: AGENT_TRANSCRIBE_PYTHON, AGENT_TRANSCRIBE_VENV, AGENT_TRANSCRIBE_BIN_DIR'
        exit 0 ;;
    *) printf '%s\n' "Unknown option: $1" >&2; exit 2 ;;
esac
[ "$#" -eq 0 ] || { printf '%s\n' 'Unexpected arguments' >&2; exit 2; }

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=${AGENT_TRANSCRIBE_PYTHON:-python3}
venv=${AGENT_TRANSCRIBE_VENV:-"$HOME/.local/share/agent-transcribe/venv"}
bin_dir=${AGENT_TRANSCRIBE_BIN_DIR:-"$HOME/bin"}
case "$venv" in
    /*) ;;
    *) printf '%s\n' 'AGENT_TRANSCRIBE_VENV must be an absolute path' >&2; exit 2 ;;
esac
case "$bin_dir" in
    /*) ;;
    *) printf '%s\n' 'AGENT_TRANSCRIBE_BIN_DIR must be an absolute path' >&2; exit 2 ;;
esac
launcher="$bin_dir/agent-transcribe"
"$python" -c 'import sys; sys.exit("Python 3.11 or later is required") if sys.version_info < (3, 11) else None'

# Reject unrelated files before changing the installation.
if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    if [ ! -L "$launcher" ] || [ "$(readlink "$launcher")" != "$venv/bin/agent-transcribe" ]; then
        printf '%s\n' "Refusing to replace unrelated launcher: $launcher" >&2
        exit 1
    fi
fi
"$python" -m venv "$venv"
package="$repo"
[ "$images" = no ] || package="$repo[images]"
"$venv/bin/python" -m pip install --editable "$package"
mkdir -p "$bin_dir"
if [ ! -L "$launcher" ]; then
    ln -s "$venv/bin/agent-transcribe" "$launcher"
fi
"$launcher" --help
printf '%s\n' "Installed: $launcher" 'Add its directory to PATH if needed. OCR executables are installed separately.'
