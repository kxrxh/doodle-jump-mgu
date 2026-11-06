# Build

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r runtime/requirements.txt
.venv/bin/python runtime/prepare.py
python3 tools/store_build.py kxrxh_doodle_jump
```

Runtime preparation uses Docker and clang. `tools/runtime.json` pins the published runtime pack. mgu-store runs only the final build command.

```sh
python3 tools/get_original.py
python3 -m unittest discover -s tools -v
cd runtime
../.venv/bin/python -m unittest discover -v
node test_controls.js
node test_client.js
node test_world.js
```
