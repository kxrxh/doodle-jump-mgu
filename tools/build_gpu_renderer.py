"""Build and package the pinned Store renderer with textured game scenes."""
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from store_build import download, safe_path


ROOT = Path(__file__).resolve().parent.parent


def extract(source, destination):
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        for member in members:
            try:
                safe_path(member.name)
            except ValueError as error:
                raise ValueError('Unsafe renderer source archive') from error
            if not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe renderer source archive entry')
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as data, target.open('wb') as output:
                    shutil.copyfileobj(data, output)
                target.chmod(member.mode & 0o777)


def package(source):
    component = ROOT / 'native/doodle-gpu'
    renderer = source / 'native/mgu-render'
    build = component / 'build'
    build.mkdir(parents=True, exist_ok=True)
    for origin, name in ((renderer / 'build/target/mgu-render', 'mgu-render'),
                         (renderer / 'LICENSE.mgu-renderd', 'LICENSE.mgu-renderd'),
                         (source / 'LICENSE', 'LICENSE.store')):
        shutil.copy2(origin, build / name)
    version = json.loads((component / 'component.json').read_text())['version']
    output = ROOT / 'dist'
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(['node', str(source / 'deploy/hu'), 'artifact', str(component), '--out', str(output)],
                   cwd=ROOT, check=True)
    sources = output / f'doodle-gpu-source-{version}.tar.gz'
    prefix = f'doodle-gpu-source-{version}'
    with sources.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as compressed, \
         tarfile.open(fileobj=compressed, mode='w') as archive:
        for path in sorted(renderer.rglob('*')):
            relative = path.relative_to(renderer)
            if not path.is_file() or any(part in ('build', '__pycache__', '.DS_Store') for part in relative.parts):
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(f'{prefix}/mgu-render/{relative.as_posix()}')
            info.size = len(data)
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            archive.addfile(info, io.BytesIO(data))
        data = (source / 'LICENSE').read_bytes()
        info = tarfile.TarInfo(f'{prefix}/LICENSE.store')
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
    return output / f'doodle-gpu@{version}.tgz', sources


def main():
    config = json.loads((ROOT / 'tools/renderer.json').read_text())
    cache = ROOT / '.cache/gpu-build'
    source = download(config['url'], config['sha256'], cache / 'store-source.tar.gz')
    checkout = cache / 'source'
    shutil.rmtree(checkout, ignore_errors=True)
    extract(source, checkout)
    source = checkout / ('mgu-store-' + config['commit'])
    patch = ROOT / 'tools/renderer-gpu.patch'
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=source, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=source, check=True)
    subprocess.run(['make', 'check-target'], cwd=source / 'native/mgu-render', check=True)
    for path in package(source):
        print(path)


if __name__ == '__main__':
    main()
