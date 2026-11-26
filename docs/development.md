# Development

Run commands from the repository root.

| Directory | Contents |
| --- | --- |
| `apps/kxrxh_doodle_jump/` | MGU app, controls, and engine client |
| `runtime/` | Python host, C++ backend, and runtime assets |
| `native/doodle-gpu/` | Store renderer add-on |
| `kit/` | MGU toolkit subset and browser emulator |
| `tools/` | Build scripts and pinned dependencies |
| `tests/` | App, runtime, and packaging tests |

Downloads, generated packages, and local previews are ignored by Git.

## Build the app

Requires Python 3, Node.js, and internet access.

```sh
python3 tools/store_build.py kxrxh_doodle_jump
```

This verifies the runtime pinned in `tools/runtime.json` and the original game
files, then creates `dist/kxrxh_doodle_jump@<version>.tgz`.
Store's Releases channel installs the ready package. Source installs use
`mgu-repo.json` and software rendering unless GPU support is already installed.

## Preview locally

Requires Python 3.13, Node.js, clang, and a C++ compiler.

```sh
./run-local.sh
```

The script downloads the verified game files, creates `.venv`, installs runtime
dependencies, and starts the browser preview. Pass a port as the first argument.

## Test

After local preview setup, run the first-party tests and toolkit integration suite.

```sh
PYTHONPATH=tools python3 -m unittest discover -s tests/tools -v
PYTHONPATH=runtime .venv/bin/python -m unittest discover -s tests/runtime -v
for suite in tests/runtime/test_*.js; do node "$suite" || exit 1; done
node kit/tools/test_runtime.js
```

## Rebuild runtime or GPU support

After runtime changes, build Linux x86-64 dependencies for Store's Python 3.12.
Requires Docker and clang. Unicorn includes only its ARM guest backend.

```sh
.venv/bin/python runtime/prepare.py
```

Publish `dist/runtime-linux-x86_64.pack` and update `tools/runtime.json` with its
URL, size, and SHA-256 before building the app.

To rebuild GPU support, use Docker, Git, and Node.js 14 or later.

```sh
python3 tools/build_gpu_renderer.py
```

The script applies `tools/renderer-gpu.patch` to the Store source pinned in
`tools/renderer.json`. It produces the add-on package and corresponding renderer
sources in `dist/`. Store installs the add-on with the game.

For browser renderer rebuilds, see [the toolkit notes](../kit/README.md).

## Publish

Upload the app package to a versioned GitHub Release. Update `catalog.json`
with its URL, size, and SHA-256, then replace the `catalog` release's catalog.
Retain licenses and corresponding sources. Keep BMW's SDK out of packages.

## Troubleshoot performance

Enable **Show FPS** in the app. **Game** measures completed engine frames and
**Render** measures renderer frames. **GPU** or **CPU** identifies the drawing
path. In CPU mode, the last line explains why GPU drawing is unavailable.
After a screen interruption, **Last** shows the most recent display event.
