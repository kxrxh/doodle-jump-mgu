"""Build private Python 3.12 dependencies and the Linux x86-64 ARM-only runtime."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parent
BUNDLE=ROOT/'bundle'
IMAGE='ubuntu:18.04@sha256:152dc042452c496007f07ca9127571cb9c29697f42acbfad72324b2bb2e43c98'
UNICORN_URL='https://github.com/kxrxh/doodle-jump-mgu/releases/download/v0.2.1/unicorn-2.1.4.tar.gz'
UNICORN_SHA256='00567a70e323f749b419cd86bee4f9115beab7ebba32194581c090cbb7c59cff'


def main():
    sys.path.insert(0,str(ROOT.parent/'tools'))
    from store_build import download
    archive=download(UNICORN_URL,UNICORN_SHA256,ROOT/'data/unicorn-2.1.4.tar.gz')
    source=ROOT/'data/unicorn-source'
    shutil.rmtree(source,ignore_errors=True)
    shutil.rmtree(ROOT/'data/unicorn-build',ignore_errors=True)
    source.mkdir(parents=True)
    with tarfile.open(archive) as package:package.extractall(source,filter='data')
    shutil.rmtree(BUNDLE,ignore_errors=True)
    BUNDLE.mkdir()
    subprocess.run([sys.executable,'-m','pip','install','--no-compile','--only-binary=:all:',
        '--platform','manylinux_2_27_x86_64','--platform','manylinux_2_24_x86_64',
        '--platform','manylinux2014_x86_64','--implementation','cp','--python-version','3.12',
        '--abi','cp312','--target',str(BUNDLE/'site-packages'),'-r',str(ROOT/'requirements.txt')],check=True)
    command=['docker','run','--rm','--platform','linux/amd64','-v',f'{ROOT}:/work','-w','/work',IMAGE]
    # Build against the MGU's glibc 2.27; newer Linux toolchains can require glibc 2.34.
    subprocess.run(command+['bash','-ec',r'''
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc make libc6-dev cmake pkg-config >/tmp/build.log
mkdir -p data/unicorn-build
cd data/unicorn-build
cmake ../unicorn-source/unicorn-2.1.4/src -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
    -DUNICORN_ARCH=arm -DUNICORN_BUILD_TESTS=OFF -DUNICORN_INSTALL=OFF \
    -DUNICORN_LEGACY_STATIC_ARCHIVE=OFF >/tmp/cmake.log
test -f config-host.h
cmake --build . -- -j2 >/tmp/unicorn.log
strip --strip-unneeded libunicorn.so.2
cp libunicorn.so.2 /work/bundle/site-packages/unicorn/lib/libunicorn.so.2
cc -x c - -I../unicorn-source/unicorn-2.1.4/src/include -L. \
    -Wl,-rpath,/work/data/unicorn-build -l:libunicorn.so.2 -o check-arm <<'C'
#include <assert.h>
#include <unicorn/unicorn.h>
int main(void) {
    assert(uc_arch_supported(UC_ARCH_ARM));
    for (int arch=UC_ARCH_ARM+1; arch<UC_ARCH_MAX; arch++) assert(!uc_arch_supported(arch));
    return 0;
}
C
./check-arm
cd /work
cc -shared -fPIC -O3 raster.c -o bundle/raster.so -lm
'''],check=True)
    library=(BUNDLE/'site-packages/unicorn/lib/libunicorn.so.2').read_bytes()
    assert library[:5]==b'\x7fELF\x02' and library[18:20]==b'\x3e\x00','Expected a Linux x86-64 library'
    for name in ('arm','libc','jni','gles','game','input_state','engine_process','commerce','audio','png_decoder','scenery'):
        shutil.copy2(ROOT/f'{name}.py',BUNDLE)
    subprocess.run(['clang','-target','armv7-none-eabi','-c',str(ROOT/'arm_helpers.S'),'-o',str(ROOT/'arm_helpers.o')],check=True)
    for name in ('arm_helpers.S','arm_helpers.o','raster.c','LICENSE','NOTICE'):
        shutil.copy2(ROOT/name,BUNDLE)
    (BUNDLE/'arm_helpers.o').touch()
    (BUNDLE/'raster.so').touch()
    (BUNDLE/'assets').mkdir()
    shutil.copy2(ROOT/'assets/cursor.png',BUNDLE/'assets/cursor.png')
    shutil.copy2(ROOT/'bundle.packignore',BUNDLE/'.packignore')
    spec=importlib.util.spec_from_file_location('pack_data',ROOT.parent/'kit/tools/pack_data.py')
    pack_data=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack_data)
    pack_data.SPLIT=1<<60
    output=ROOT.parent/'dist/runtime-linux-x86_64.pack'
    output.parent.mkdir(exist_ok=True)
    raw,size,count=pack_data.pack(str(BUNDLE),str(output))
    print(f'{output.name}: {count} files, {raw/1e6:.1f} MB extracted, {size/1e6:.1f} MB packed')


if __name__=='__main__':main()
