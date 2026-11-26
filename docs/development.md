# Development

Run commands from the repository root. Builds happen off-device; MGU runs the
prebuilt x86-64 host and a small Rust/WASM module through Store's JavaScript SDK.

| Directory | Contents |
| --- | --- |
| `apps/kxrxh_doodle_jump/` | Store launcher and renderer/socket adapters |
| `runtime/host/` | Native Rust host: assets, JNI, audio and engine lifecycle |
| `runtime/ui/` | Rust/WASM controls and side scenery |
| `runtime/recomp/` | ARM-to-x86-64 translator and iparecomp provenance |
| `runtime/*.cpp` | C++ drawing, mixer and frame encoding |
| `native/doodle-gpu/` | Independent EGL/Wayland presentation worker |
| `kit/` | MGU toolkit subset and browser emulator |
| `tools/`, `tests/` | Build, comparison and verification tools |

Generated files, downloads and previews stay in ignored directories.

## Build or preview

To package the pinned release runtime, install Python 3 and run:

```sh
python3 tools/store_build.py kxrxh_doodle_jump
```

This verifies the runtime and original game downloads and creates
`dist/kxrxh_doodle_jump@<version>.tgz`. Store installs that ready package.

For source development, install Python 3.12+, Node.js, Docker and clang, then run:

```sh
./run-local.sh 8079
```

The script prepares a development environment, builds the Linux host and WASM,
and opens the browser emulator. The native host runs in Docker with a 512 MiB
limit. No Python interpreter or compiler is required on the MGU.

## Rebuild and test

After preview setup:

```sh
.venv/bin/python runtime/prepare.py
.venv/bin/python tools/build_ui.py
.venv/bin/python tools/build_host.py --command test
.venv/bin/python tools/build_host.py --command clippy
.venv/bin/python tools/build_ui.py --check
PYTHONPATH=tools .venv/bin/python -m unittest discover -s tests/tools -v
for suite in tests/runtime/test_*.js; do node "$suite" || exit 1; done
node kit/tools/test_runtime.js
```

The build scripts fetch a verified Rust SDK and miniaudio sources, compile against
glibc 2.27, and produce `dist/runtime-linux-x86_64.pack`. Runtime packaging includes
licenses and excludes Python, Unicorn, Cargo and build sources. Update the URL,
size and SHA-256 in `tools/runtime.json` before packaging a new release.

The ARM library supplies original data; translated game logic runs as x86-64
code in `recomp/game.so`. All 12 themes and original sound assets are retained.

## Compare against the previous host

The Python host is an external, SHA-pinned v0.12.1 development oracle. Prepare it
without maintaining a second host in this repository:

```sh
.venv/bin/python tools/prepare_reference.py
.venv/bin/python tools/prepare_unicorn.py
```

Run these suites in Linux x86-64 with Store's CPython 3.12. Backend tests use the
current C++ libraries; frozen-host tests use the immutable release. Run each suite
in a separate Python process:

```sh
python3 -m unittest discover -s tests/reference/backends -v
python3 -m unittest discover -s tests/recomp -v
python3 -m unittest discover -s tests/host -v
python3 tools/compare_hosts.py --mode parity
python3 tools/compare_hosts.py --mode benchmark --themes 1 10
```

Comparisons cover frame/texture commands, saves, audio events and decoded samples.
Benchmarks report uncapped host throughput, frame times, startup and peak process
RSS. They exclude the GPU worker and physical display, so they do not establish
real MGU FPS. Use the packaged Node 8/EGL pipeline and real hardware to check
presentation and frame pacing.

## GPU support and publishing

```sh
python3 tools/build_gpu_renderer.py
make -C native/doodle-gpu test
```

The worker uses public Wayland/IVI interfaces and the game's compositor layer;
Store keeps ownership of visibility and input. A VIDEO fallback handles systems
without native presentation. No shared renderer override is installed.

Upload app/runtime artifacts to a versioned GitHub Release. Update `catalog.json`
with the verified artifact URL, size and SHA-256, then replace the `catalog`
release's catalog. Retain required licenses and corresponding GPU worker sources.
Keep BMW's SDK out of packages.

For performance diagnosis, enable **Show FPS**. **Game** measures completed engine
frames; **Render** measures renderer frames; **Native** counts compositor callbacks.
**CPU** shows why GPU drawing is unavailable, and **Last** records display events.
