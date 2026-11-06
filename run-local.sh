#!/bin/sh
set -eu
doodle_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$doodle_root"
python3 tools/get_original.py
if [ ! -x .venv/bin/python ]; then python3.13 -m venv .venv; fi
.venv/bin/python -m pip install -q -r runtime/requirements.txt
export DOODLE_ENGINE="$doodle_root/runtime"
export DOODLE_PYTHON="$doodle_root/.venv/bin/python"
exec node kit/tools/emu.js kxrxh_doodle_jump --port "${1:-8079}"
