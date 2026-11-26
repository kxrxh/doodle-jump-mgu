# Development

Run commands from the repository root.

| Directory | Contents |
| --- | --- |
| `apps/kxrxh_doodle_jump/` | MGU app, controls, and engine client |
| `runtime/` | Python host, C++ backend, and runtime assets |
| `native/doodle-gpu/` | Independent GPU presentation worker |
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

To rebuild the GPU worker, use Docker and Python 3.

```sh
python3 tools/build_gpu_renderer.py
```

The worker presents through a Wayland/EGL surface on the game's existing
compositor layer. It binds the layer through a temporary placement challenge
and renderer peer credentials, using public IVI interfaces. Store owns the
parent layer's visibility and input handling. The worker provides a VIDEO
fallback when native presentation is unavailable. Its service and socket
belong to `doodle-gpu`. The build creates
the worker package and corresponding sources in `dist/`; Store installs it with
the game. Upgrading an older GPU add-on removes its renderer override. Restart
when Store asks to complete that migration.

Run the worker's native integration tests with Mesa before publishing.

```sh
make -C native/doodle-gpu test
```

For browser renderer rebuilds, see [the toolkit notes](../kit/README.md).

## Publish

Upload the app package to a versioned GitHub Release. Update `catalog.json`
with its URL, size, and SHA-256, then replace the `catalog` release's catalog.
Retain licenses and corresponding sources. Keep BMW's SDK out of packages.

## Troubleshoot performance

Enable **Show FPS** in the app. **Game** measures completed engine frames and
**Render** measures renderer frames. **Native** counts compositor frame
callbacks. **Video** uses the shared video transport, and **CPU** uses software
rasterization. In CPU mode, the last line explains why GPU drawing is unavailable.
After a screen interruption, **Last** shows the most recent display event.
