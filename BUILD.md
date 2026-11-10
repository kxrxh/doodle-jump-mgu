# Build

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r runtime/requirements.txt
.venv/bin/python runtime/prepare.py
python3 tools/store_build.py kxrxh_doodle_jump
```

Runtime preparation uses Docker and clang. `tools/runtime.json` pins the published runtime pack. Run the final build command on the build computer to prepare a release; the Releases installation uses the ready package. A `main` branch installation runs that command on the head unit and needs internet.

Publish `dist/kxrxh_doodle_jump@<version>.tgz` as a versioned GitHub Release asset. Update `catalog.json` with its version, download URL, byte size and SHA-256, then upload the catalog and `apps/kxrxh_doodle_jump/icon.png` (named `kxrxh_doodle_jump.png`) to the release named `catalog`. The catalog has no `build` field or controller-gate dependency. Keep BMW's SDK out of published packages and preserve runtime licenses and corresponding source releases.

```sh
python3 tools/get_original.py
python3 -m unittest discover -s tools -v
cd runtime
../.venv/bin/python -m unittest discover -v
node test_controls.js
node test_client.js
node test_world.js
```
