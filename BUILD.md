# Development

## Build the app

Requires Python 3, Node.js and internet:

```sh
python3 tools/store_build.py kxrxh_doodle_jump
```

Uses the verified runtime pinned in `tools/runtime.json` and original game files.
Output: `dist/kxrxh_doodle_jump@<version>.tgz`. Store's Releases channel installs
this ready package; source installs use `mgu-repo.json`.

## Rebuild the runtime

Only needed after runtime changes. Requires Docker and clang:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r runtime/requirements.txt
.venv/bin/python runtime/prepare.py
```

Builds Linux x86-64 dependencies for Store's Python 3.12, with ARM-only Unicorn
and Ubuntu 18.04 compatibility. Publish the runtime pack and update its pin.

## Preview and test

Run `./run-local.sh` to set up local dependencies and open the preview.
Requires clang and a C++ compiler. After setup, run the tests:

```sh
python3 -m unittest discover -s tools -v
.venv/bin/python -m unittest discover -s runtime -v
node runtime/test_controls.js
node runtime/test_client.js
node runtime/test_pacing.js
node runtime/test_world.js
```

## Publish

Upload the app package to a versioned GitHub Release. Update `catalog.json`
with its URL, size and SHA-256, then replace the `catalog` release's catalog.
Keep BMW's SDK out of packages and retain licenses and corresponding sources.
