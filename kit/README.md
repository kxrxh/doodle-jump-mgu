# MGU toolkit

This directory contains the toolkit components used to build, run, and preview
Doodle Jump. Upstream revision and attribution are recorded in [NOTICE](../NOTICE).
Toolkit code retains its [MIT license](LICENSE).

The browser preview uses the committed WebAssembly renderer in `emu/web/gfx/`.
The renderer and `emu/wasm/` adapter are GPL-3.0-or-later, with the original
renderer's [MIT notice](LICENSE.mgu-renderd). The GPL text is in
[`runtime/licenses/GPL-3.txt`](../runtime/licenses/GPL-3.txt).

To rebuild WebAssembly, fetch the matching upstream renderer sources first.
Requires Git and Docker with the pinned Emscripten image.

```sh
git clone https://github.com/cauliflowermossgrape/idrive7-apps .cache/idrive7-apps
git -C .cache/idrive7-apps checkout cca7f9ad17f96b92e60a89ea98686afe1b662041
kit/emu/wasm/build.sh
```

Run these commands from the repository root. `MGU_RENDERD_SOURCE` accepts a
renderer source directory relative to that root. The build replaces the
committed WebAssembly outputs only after compilation succeeds.
