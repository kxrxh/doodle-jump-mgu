"""Package the host and menu update for a verified v0.14.1 Store install."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile

from build_gpu_renderer import archive
from store_build import ROOT, digest

sys.path.insert(0, str(ROOT / 'kit/tools'))
from build import build_ui, unicode_resources


APP = 'kxrxh_doodle_jump'
BASE_SHA256 = '9cbb5f791b0eb4982c85d0007ed5ff99d883b9a02ad12baa99c926c1cde678c8'
ORIGINAL_HOST = '04ac2ca6c02a37b21b4f9c0578ddf22c3eee2ac199914751867d45ec826341d0'
PREVIOUS_HOST = '2b1c6c5412950fc1a53d6ba46fe6a5a71387d417308da47ad52caf221faa4d34'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pack_strings(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_app', type=Path)
    parser.add_argument('host', type=Path)
    parser.add_argument('licenses', type=Path)
    args = parser.parse_args()
    if digest(args.base_app) != BASE_SHA256:
        raise SystemExit('Expected the verified v0.14.1 Store package')
    if not all((args.licenses / name).is_dir() for name in ('rust-crates', 'rust-std')):
        raise SystemExit('Expected the native runtime licenses directory')

    app_dir = ROOT / 'apps' / APP
    spec = json.loads((app_dir / 'app.json').read_text())
    xml, ui, strings, states = build_ui(spec)
    unicode_resources(ui, strings, 'en')
    with tarfile.open(args.base_app, 'r:gz') as package:
        def shipped(name):
            return package.extractfile('package/' + name).read()

        runtime_spec = json.loads(shipped('javascript/kit/app-spec.json'))
        runtime_spec.update(version=spec['version'], settings=spec['settings'], stateIds=states)
        files = {
            'app.js': ('javascript/app.js', (app_dir / 'main.js').read_bytes()),
            'app-spec.json': ('javascript/kit/app-spec.json', json.dumps(runtime_spec, indent=1).encode()),
            'application.xml': ('rhmiData/uiDescriptions/ID7/application.xml', xml.encode()),
            'uiDescription.json': ('rhmiData/uiDescriptions/ID7/uiDescription.json', json.dumps(ui, indent=1).encode()),
        }
        index = {name: {'original': sha256(shipped(source)), 'updated': sha256(new)}
                 for name, (source, new) in files.items()}
        engine_sha256 = sha256(shipped('javascript/engine.pack'))
        locale_dir = 'rhmiData/variantGroups/Common/texts/'
        base_strings = json.loads(shipped(locale_dir + 'en_GB.json'))
        for member in package.getmembers():
            if member.isfile() and member.name.startswith('package/' + locale_dir):
                if json.load(package.extractfile(member)) != base_strings:
                    raise SystemExit('Expected matching string bundles across base app locales')

    host = args.host.read_bytes()
    index['doodle-host'] = {'original': ORIGINAL_HOST, 'previous': PREVIOUS_HOST, 'updated': sha256(host)}
    new_strings = pack_strings(strings)
    index['strings.json'] = {'original': sha256(pack_strings(base_strings)), 'updated': sha256(new_strings)}
    contents = [(name, new, 0o644) for name, (_, new) in files.items()]
    contents.extend([
        ('doodle-host', host, 0o755),
        ('strings.json', new_strings, 0o644),
        ('patch.json', (json.dumps({'engine_sha256': engine_sha256, 'files': index}, indent=2) + '\n').encode(), 0o644),
        ('install.js', (ROOT / 'tools/install_host_patch.js').read_bytes(), 0o644),
        ('LICENSE', (ROOT / 'LICENSE').read_bytes(), 0o644),
        ('NOTICE', (ROOT / 'NOTICE').read_bytes(), 0o644),
    ])
    for file in sorted(args.licenses.rglob('*')):
        if file.is_file():
            contents.append((f'licenses/{file.relative_to(args.licenses).as_posix()}', file.read_bytes(), 0o644))

    name = f"doodle-app-update-v{spec['version']}"
    output = ROOT / 'dist' / f'{name}-linux-x86_64.tar.gz'
    output.parent.mkdir(exist_ok=True)
    archive(output, [(f'{name}/{file}', data, mode) for file, data, mode in contents])
    print(output)


if __name__ == '__main__':
    main()
