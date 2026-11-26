"""Build the Linux x86-64 game module and private Python 3.12 dependencies."""
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


def apply_unicorn_patch(source):
    lines=(ROOT/'unicorn-write-protect.patch').read_text().splitlines(keepends=True)
    if sum(line.startswith('@@ ') for line in lines)!=1:
        raise ValueError('Expected one pinned Unicorn patch hunk')
    before=''.join(line[1:] for line in lines[3:] if line.startswith((' ','-')))
    after=''.join(line[1:] for line in lines[3:] if line.startswith((' ','+')))
    target=source/'unicorn-2.1.4/src/qemu/accel/tcg/cputlb.c'
    text=target.read_text()
    if not before or text.count(before)!=1:
        raise ValueError('Unicorn write-protection patch does not match the pinned source')
    target.write_text(text.replace(before,after,1))


def main():
    original=ROOT.parent/'original/extracted/lib/armeabi-v7a/libDoodleJump.so'
    if not original.is_file():
        raise SystemExit('Verified game engine missing; run python3 tools/get_original.py first')
    generated=ROOT/'data/recomp-build'
    shutil.rmtree(generated,ignore_errors=True)
    subprocess.run([sys.executable,str(ROOT.parent/'tools/recompile.py'),'--report',
        '--original',str(original),'--output',str(generated)],check=True)
    sys.path.insert(0,str(ROOT.parent/'tools'))
    from store_build import download
    archive=download(UNICORN_URL,UNICORN_SHA256,ROOT/'data/unicorn-2.1.4.tar.gz')
    source=ROOT/'data/unicorn-source'
    shutil.rmtree(source,ignore_errors=True)
    shutil.rmtree(ROOT/'data/unicorn-build',ignore_errors=True)
    source.mkdir(parents=True)
    with tarfile.open(archive) as package:package.extractall(source,filter='data')
    apply_unicorn_patch(source)
    shutil.rmtree(BUNDLE,ignore_errors=True)
    BUNDLE.mkdir()
    (BUNDLE/'recomp').mkdir()
    subprocess.run([sys.executable,'-m','pip','install','--no-compile','--only-binary=:all:',
        '--platform','manylinux_2_27_x86_64','--platform','manylinux_2_24_x86_64',
        '--platform','manylinux2014_x86_64','--implementation','cp','--python-version','3.12',
        '--abi','cp312','--target',str(BUNDLE/'site-packages'),'-r',str(ROOT/'requirements.txt')],check=True)
    command=['docker','run','--rm','--platform','linux/amd64','-v',f'{ROOT}:/work','-w','/work',IMAGE]
    # Build against the MGU's glibc 2.27; newer Linux toolchains can require glibc 2.34.
    subprocess.run(command+['bash','-ec',r'''
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc g++ make libc6-dev cmake pkg-config >data/build-toolchain.log 2>&1 || {
    cat data/build-toolchain.log
    exit 1
}
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
cc -c -fPIC -O3 raster.c -o data/raster.o
g++ -std=c++14 -shared -fPIC -O3 -pthread -static-libstdc++ -static-libgcc \
    dispatch.cpp image_memory.cpp graphics.cpp audio.cpp frame.cpp raster_parallel.cpp data/raster.o -o bundle/native.so
strip --strip-unneeded bundle/native.so
for source in data/recomp-build/*.c; do
    cc -std=c11 -fPIC -fexceptions -fno-strict-aliasing -ffp-contract=off -O2 \
        -Idata/recomp-build -Irecomp -c "$source" -o "${source%.c}.o"
done
g++ -std=c++14 -fPIC -fexceptions -fno-strict-aliasing -ffp-contract=off -O2 \
    -Idata/recomp-build -Irecomp -c recomp/guest.cpp -o data/recomp-build/guest.o
g++ -shared -static-libstdc++ -static-libgcc data/recomp-build/*.o -lm -o bundle/recomp/game.so
strip --strip-unneeded bundle/recomp/game.so
'''],check=True)
    for path in (BUNDLE/'site-packages/unicorn/lib/libunicorn.so.2',BUNDLE/'recomp/game.so'):
        library=path.read_bytes()
        assert library[:5]==b'\x7fELF\x02' and library[18:20]==b'\x3e\x00','Expected a Linux x86-64 library'
    for name in ('arm','libc','jni','gles','game','input_state','engine_process','commerce','audio','png_decoder','scenery','native_backend','frame_encoder'):
        shutil.copy2(ROOT/f'{name}.py',BUNDLE)
    for name in ('arm_helpers.S','arm_helpers.o','raster.c','texture.h','dispatch.cpp','dispatch.h','image_memory.cpp','image_memory.h','graphics.cpp','audio.cpp','frame.cpp','raster_parallel.cpp','prepare.py','unicorn-write-protect.patch','LICENSE'):
        shutil.copy2(ROOT/name,BUNDLE)
    for name in ('__init__.py','machine.py','LICENSE','upstream.json'):
        shutil.copy2(ROOT/'recomp'/name,BUNDLE/'recomp'/name)
    shutil.copy2(ROOT.parent/'NOTICE',BUNDLE/'NOTICE')
    shutil.copytree(ROOT/'licenses',BUNDLE/'licenses')
    (BUNDLE/'arm_helpers.o').touch()
    (BUNDLE/'native.so').touch()
    from native_backend import source_digest
    (BUNDLE/'native.so.sha256').write_text(source_digest(BUNDLE)+'\n')
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
