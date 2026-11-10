"""Load the packaged C++ backend; compile it only for local development."""
import ctypes
from functools import lru_cache
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile


def source_digest(directory):
    checksum = hashlib.sha256()
    for path in sorted([*directory.glob('*.cpp'), *directory.glob('*.h'),
                        directory / 'raster.c', directory / 'native_backend.py']):
        checksum.update(path.name.encode() + b'\0' + path.read_bytes())
    return checksum.hexdigest()


@lru_cache(maxsize=1)
def library():
    directory = Path(__file__).resolve().parent
    target = directory / ('native.dylib' if sys.platform == 'darwin' else 'native.so')
    sources = sorted(directory.glob('*.cpp'))
    marker = target.with_suffix(target.suffix + '.sha256')
    digest = source_digest(directory)
    # Pack extraction changes timestamps; content identifies a matching prebuilt backend.
    if not target.exists() or not marker.exists() or marker.read_text().strip() != digest:
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            raster = Path(temporary) / 'raster.o'
            output = Path(temporary) / target.name
            subprocess.run(['cc', '-c', '-fPIC', '-O3', str(directory / 'raster.c'), '-o', str(raster)], check=True)
            flags = [] if sys.platform == 'darwin' else ['-static-libstdc++', '-static-libgcc']
            subprocess.run(['c++', '-std=c++14', '-shared', '-fPIC', '-O3', '-pthread',
                            *flags, *(str(path) for path in sources), str(raster), '-o', str(output)], check=True)
            output.replace(target)
            marker.write_text(digest + '\n')
    return ctypes.CDLL(str(target))
