"""Build the portable x86-64 Linux runtime without original game files."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


ROOT=Path(__file__).resolve().parent
BUNDLE=ROOT/'bundle'
URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
SHA256='df15fe60d3ff55bf9dcf25824b5572756bb8816ff93865a273d3301d7066a8b7'
IMAGE='python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72'


def main():
    BUNDLE.mkdir(exist_ok=True)
    archive=ROOT/'data/python-linux.tar.gz'
    if not (BUNDLE/'python/bin/python3.13').exists():
        archive.parent.mkdir(exist_ok=True)
        if not archive.exists():
            urllib.request.urlretrieve(URL,archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA256:
            raise ValueError('Python distribution digest mismatch')
        with tarfile.open(archive) as source:
            source.extractall(BUNDLE,filter='data')
    command=['docker','run','--rm','--platform','linux/amd64','-v',f'{ROOT}:/work','-w','/work',IMAGE]
    subprocess.run(command+['/work/bundle/python/bin/python3.13','-m','pip','install','--no-cache-dir','-r','requirements.txt'],check=True)
    if not (BUNDLE/'raster.so').exists() or (ROOT/'raster.c').stat().st_mtime>(BUNDLE/'raster.so').stat().st_mtime:
        subprocess.run(command+['sh','-c','apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc libc6-dev >/tmp/build.log && cc -shared -fPIC -O3 raster.c -o bundle/raster.so -lm'],check=True)
    for name in ('arm','libc','jni','gles','game','input_state','engine_process','commerce','audio','png_decoder','scenery'):
        shutil.copy2(ROOT/f'{name}.py',BUNDLE)
    subprocess.run(['clang','-target','armv7-none-eabi','-c',str(ROOT/'arm_helpers.S'),'-o',str(ROOT/'arm_helpers.o')],check=True)
    for name in ('arm_helpers.S','arm_helpers.o'):
        shutil.copy2(ROOT/name,BUNDLE)
    (BUNDLE/'arm_helpers.o').touch()
    shutil.copy2(ROOT/'raster.c',BUNDLE)
    # The device uses the prebuilt rasterizer; it does not need a compiler.
    (BUNDLE/'raster.so').touch()
    shutil.copy2(ROOT/'LICENSE',BUNDLE)
    shutil.copy2(ROOT/'NOTICE',BUNDLE)
    (BUNDLE/'assets').mkdir(exist_ok=True)
    shutil.copy2(ROOT/'assets/cursor.png',BUNDLE/'assets/cursor.png')
    shutil.copy2(ROOT/'bundle.packignore',BUNDLE/'.packignore')
    kit=ROOT.parent/'kit/tools/pack_data.py'
    spec=importlib.util.spec_from_file_location('pack_data',kit)
    pack_data=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack_data)
    pack_data.SPLIT=1<<60
    output=ROOT.parent/'dist/runtime-linux-x86_64.pack'
    output.parent.mkdir(exist_ok=True)
    raw,size,count=pack_data.pack(str(BUNDLE),str(output))
    print(f'{output.name}: {count} files, {raw/1e6:.1f} MB extracted, {size/1e6:.1f} MB packed')


if __name__=='__main__':
    main()
