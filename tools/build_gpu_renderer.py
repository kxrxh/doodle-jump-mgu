"""Build and package the independent Doodle Jump GPU worker."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from store_build import safe_path


ROOT = Path(__file__).resolve().parent.parent


def archive(destination, entries):
    with destination.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as compressed, \
         tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as output:
        for name, data, mode in sorted(entries):
            safe_path(name)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            output.addfile(info, io.BytesIO(data))


def package():
    component = ROOT / 'native/doodle-gpu'
    specification = json.loads((component / 'component.json').read_text())
    manifest = {key: value for key, value in specification.items() if key not in ('files', 'units', 'build')}
    entries = []
    manifest['files'] = []
    for entry in specification['files']:
        safe_path(entry['path'])
        origin = entry.get('from', entry['path'])
        safe_path(origin)
        data = (component / origin).read_bytes()
        mode = entry.get('mode', '0644')
        manifest['files'].append(dict(path=entry['path'], mode=mode, sha256=hashlib.sha256(data).hexdigest()))
        entries.append(('files/' + entry['path'], data, int(mode, 8)))
    manifest['units'] = []
    for entry in specification['units']:
        safe_path(entry['name'])
        origin = entry.get('from', entry['name'])
        safe_path(origin)
        manifest['units'].append(dict(name=entry['name']))
        entries.append(('units/' + entry['name'], (component / origin).read_bytes(), 0o644))
    entries.append(('component.json', (json.dumps(manifest, indent=2) + '\n').encode(), 0o644))
    output = ROOT / 'dist'
    output.mkdir(parents=True, exist_ok=True)
    version = specification['version']
    artifact = output / f'doodle-gpu@{version}.tgz'
    archive(artifact, entries)
    prefix = f'doodle-gpu-source-{version}'
    sources = output / f'{prefix}.tar.gz'
    entries = []
    for path in sorted(component.rglob('*')):
        relative = path.relative_to(component)
        if not path.is_file() or any(part in ('build', '__pycache__', '.DS_Store') for part in relative.parts):
            continue
        entries.append((f'{prefix}/{relative.as_posix()}', path.read_bytes(), 0o644))
    archive(sources, entries)
    return artifact, sources


def main():
    subprocess.run(['make', 'target'], cwd=ROOT / 'native/doodle-gpu', check=True)
    for path in package():
        print(path)


if __name__ == '__main__':
    main()
