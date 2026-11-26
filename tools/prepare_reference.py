"""Prepare the pinned Python release as an external development oracle."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/reference'
BASELINE = CACHE / 'baseline'
ENGINE = CACHE / 'engine'
URL = 'https://github.com/kxrxh/doodle-jump-mgu/releases/download/v0.12.1/runtime-linux-x86_64.pack'
SHA256 = '56e621ea722035783bdde0758a08c81afb92f529522d77f95ab94fbc82b05375'
ORIGINAL_SHA256 = '4f92175d8ca5fcab546c0170803f80f7e7b37873b7c51241ef5a12c703fd8158'
_MARKER = '.reference.json'
_HOST_MODULES = ('arm', 'audio', 'game', 'gles', 'jni', 'libc', 'native_backend', 'scenery', 'frame_encoder')


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def _inflate(data, limit):
    decoder = zlib.decompressobj(-15)
    output = decoder.decompress(data, limit + 1)
    if len(output) > limit or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid or oversized reference pack member')
    return output


def unpack(pack, destination, expected=SHA256):
    """Verify before extracting, and publish only a complete cache directory."""
    pack, destination = Path(pack), Path(destination)
    if digest(pack) != expected:
        raise ValueError('Reference runtime SHA-256 mismatch')
    marker = destination / _MARKER
    if destination.exists():
        if destination.is_symlink() or not marker.is_file() or json.loads(marker.read_text()).get('sha256') != expected:
            raise ValueError('Reference destination is not a verified cache')
        return destination
    with pack.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:8] != b'MGUPACK1':
            raise ValueError('Invalid reference runtime pack')
        size = struct.unpack_from('<I', header, 8)[0]
        if not 0 < size <= 1 << 20:
            raise ValueError('Invalid reference pack index size')
        index = json.loads(_inflate(stream.read(size), 8 << 20))
        files = index.get('files') if isinstance(index, dict) else None
        if not isinstance(files, dict) or not files or len(files) > 4096:
            raise ValueError('Invalid reference file index')
        base = 12 + size
        payload = pack.stat().st_size - base
        paths = set()
        for name, entry in files.items():
            path = PurePosixPath(name)
            if (not isinstance(name, str) or not name or path.is_absolute() or '..' in path.parts
                    or '\\' in name or '\0' in name or str(path) in ('.', _MARKER) or str(path) in paths
                    or not isinstance(entry, list) or len(entry) != 2
                    or any(type(value) is not int for value in entry)
                    or entry[0] < 0 or entry[1] <= 0 or entry[1] > 64 << 20
                    or entry[0] + entry[1] > payload):
                raise ValueError('Unsafe reference pack entry')
            paths.add(str(path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='reference-', dir=destination.parent) as temporary:
            temporary = Path(temporary)
            total = 0
            for name, (offset, count) in files.items():
                stream.seek(base + offset)
                contents = _inflate(stream.read(count), 64 << 20)
                total += len(contents)
                if total > 256 << 20:
                    raise ValueError('Reference runtime exceeds its extraction limit')
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
            (temporary / _MARKER).write_text(json.dumps(dict(version='0.12.1', sha256=expected)) + '\n')
            temporary.replace(destination)
    return destination


def _copy(source, destination):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and source.read_bytes() == destination.read_bytes():
        return
    temporary = destination.with_name(destination.name + '.tmp')
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def overlay(baseline, destination, native, game, source=ROOT / 'runtime'):
    """Frozen Python adapters exercise current C++ binaries in a separate cache."""
    baseline, destination, native, game, source = map(Path, (baseline, destination, native, game, source))
    if baseline.resolve() == destination.resolve() or destination.is_symlink():
        raise ValueError('The current backend cache must be separate from the frozen release')
    if not (native / 'native.so').is_file() or not game.is_file():
        raise FileNotFoundError('Build the native runtime before preparing backend tests')
    if not destination.exists():
        shutil.copytree(baseline, destination)
    elif not (destination / _MARKER).is_file():
        raise ValueError('Backend destination is not a reference cache')
    sources = [*source.glob('*.cpp'), *source.glob('*.h'), source / 'raster.c']
    names = {path.name for path in sources}
    for old in [*destination.glob('*.cpp'), *destination.glob('*.h')]:
        if old.name not in names:
            old.unlink()
    for path in sources:
        _copy(path, destination / path.name)
    for name in ('arm_helpers.S', 'arm_helpers.o'):
        if (source / name).is_file():
            _copy(source / name, destination / name)
    _copy(native / 'native.so', destination / 'native.so')
    _copy(game, destination / 'recomp/game.so')
    checksum = hashlib.sha256()
    for path in sorted([*destination.glob('*.cpp'), *destination.glob('*.h'),
                        destination / 'raster.c', destination / 'native_backend.py']):
        checksum.update(path.name.encode() + b'\0' + path.read_bytes())
    (destination / 'native.so.sha256').write_text(checksum.hexdigest() + '\n')
    (destination / '.backends.json').write_text(json.dumps(dict(native=digest(native / 'native.so'), game=digest(game))) + '\n')
    return destination


def _assets(baseline, original):
    if (baseline / 'original').is_dir():
        return
    library = original / 'extracted/lib/armeabi-v7a/libDoodleJump.so'
    if not library.is_file() or digest(library) != ORIGINAL_SHA256:
        raise ValueError('Verified original game missing; run tools/get_original.py first')
    with tempfile.TemporaryDirectory(prefix='original-', dir=baseline.parent) as temporary:
        temporary = Path(temporary)
        shutil.copytree(original, temporary / 'original')
        (temporary / 'original').replace(baseline / 'original')


def _tools():
    requirements = ROOT / 'tools/requirements.txt'
    destination = CACHE / 'site-packages'
    marker = destination / '.requirements.sha256'
    checksum = digest(requirements)
    if marker.is_file() and marker.read_text().strip() == checksum:
        return
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-compile', '--no-deps', '--only-binary=:all:',
        '--platform', 'manylinux2014_x86_64', '--implementation', 'cp', '--python-version', '3.12', '--abi', 'cp312',
        '--target', str(destination), '-r', str(requirements)], check=True,
        env=dict(os.environ, PIP_CACHE_DIR=str(CACHE / 'pip')))
    marker.write_text(checksum + '\n')


def activate(current=True, directory=None):
    """Use a prepared oracle; importing tests never downloads or compiles anything."""
    if directory is None:
        directory = Path(os.environ.get('DOODLE_REFERENCE', BASELINE)) if not current else ENGINE
    directory = Path(directory).resolve()
    if not (directory / 'arm.py').is_file():
        raise RuntimeError('Reference runtime missing; run python tools/prepare_reference.py first')
    if directory in (BASELINE.resolve(), ENGINE.resolve()):
        marker = directory / _MARKER
        if not marker.is_file() or json.loads(marker.read_text()).get('sha256') != SHA256:
            raise RuntimeError('Reference cache does not match the pinned release')
    for name in _HOST_MODULES:
        module = sys.modules.get(name)
        if module is not None and not Path(module.__file__).resolve().is_relative_to(directory):
            raise RuntimeError('Run frozen-host and current-backend tests in separate Python processes')
    paths = [directory, directory / 'site-packages', ROOT / 'runtime', CACHE / 'site-packages',
             ROOT / 'runtime/data/oracle/site-packages']
    for path in reversed(paths):
        value = str(path)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
    package = importlib.import_module('recomp')
    for path in (directory / 'recomp', ROOT / 'runtime/recomp'):
        if str(path) not in package.__path__:
            package.__path__.append(str(path))
    sys.dont_write_bytecode = True
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path, help='use a local copy of the pinned release pack')
    parser.add_argument('--original', type=Path, default=ROOT / 'original')
    parser.add_argument('--native-dir', type=Path, default=ROOT / 'runtime/bundle')
    parser.add_argument('--game-library', type=Path)
    parser.add_argument('--baseline-only', action='store_true', help='fetch the frozen runtime without current-backend setup')
    args = parser.parse_args()
    if args.pack is None:
        from store_build import download
        args.pack = download(URL, SHA256, CACHE / 'runtime-0.12.1.pack')
    unpack(args.pack, BASELINE)
    if args.original.is_dir():
        _assets(BASELINE, args.original)
    elif not args.baseline_only:
        raise SystemExit('Verified original game missing; run tools/get_original.py first')
    if not args.baseline_only:
        game = args.game_library or args.native_dir / 'recomp/game.so'
        overlay(BASELINE, ENGINE, args.native_dir, game)
        _tools()
    print(json.dumps(dict(baseline=str(BASELINE), backends=str(ENGINE))))


if __name__ == '__main__':
    main()
