#!/bin/sh
set -eu
doodle_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$doodle_root"
if [ ! -x .venv/bin/python ]; then
    doodle_python=$(command -v python3.13 || command -v python3.12 || command -v python3)
    "$doodle_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Local builds require Python 3.12 or newer")'
    "$doodle_python" -m venv .venv
fi
.venv/bin/python -m pip install -q -r tools/requirements.txt
exec .venv/bin/python tools/run_local.py --port "${1:-8079}"
