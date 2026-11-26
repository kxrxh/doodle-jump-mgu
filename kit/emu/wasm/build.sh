#!/bin/bash
# Build the browser renderer with the upstream sources described in kit/README.md.
# Emscripten's cache lives outside the repository and can be set with EM_CACHE_DIR.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
IMAGE=${EMSDK_IMAGE:-emscripten/emsdk:6.0.12}
CACHE=${EM_CACHE_DIR:-$HOME/.cache/mgu-emu-emcache}
OUT=kit/emu/web/gfx
SRC=${MGU_RENDERD_SOURCE:-.cache/idrive7-apps/kit/renderd/src}
if [ ! -f "$ROOT/$SRC/gfx.c" ]; then
    printf '%s\n' 'Renderer sources missing. Follow kit/README.md to fetch the pinned source.' >&2
    exit 1
fi
TMP=$OUT/.build-$$                    # the outputs replace the committed ones only when the build succeeds
mkdir -p "$ROOT/$TMP" "$CACHE"
trap 'rm -rf "$ROOT/$TMP"' EXIT
docker run --rm -u "$(id -u):$(id -g)" -e EM_CACHE=/emcache -v "$CACHE:/emcache" -v "$ROOT:/src" -w /src "$IMAGE" \
    emcc -O3 -g0 -std=gnu11 -D_GNU_SOURCE -Wall -Wextra -Wno-unused-parameter -Wno-misleading-indentation -Werror \
    -I"$SRC" kit/emu/wasm/emu_gfx.c "$SRC/gfx.c" "$SRC/dialect.c" "$SRC/log.c" \
    --js-library kit/emu/wasm/emu_lib.js \
    -sMODULARIZE -sEXPORT_ES6 -sEXPORT_NAME=createMguGfxWasm -sENVIRONMENT=web \
    -sMIN_WEBGL_VERSION=2 -sMAX_WEBGL_VERSION=2 \
    -sALLOW_MEMORY_GROWTH -sINITIAL_MEMORY=64MB -sFILESYSTEM=0 \
    -sEXPORTED_FUNCTIONS=_emu_init,_emu_error,_emu_buf,_emu_feed,_emu_reset,_emu_read,_emu_stats,_emu_app,_emu_err,_emu_destroy \
    -sEXPORTED_RUNTIME_METHODS=HEAPU8,HEAP32,HEAPF64,UTF8ToString \
    -o $TMP/mgu-gfx-wasm.js
mv "$ROOT/$TMP/mgu-gfx-wasm.js" "$ROOT/$TMP/mgu-gfx-wasm.wasm" "$ROOT/$OUT/"
ls -l "$ROOT/$OUT"/mgu-gfx-wasm.*
